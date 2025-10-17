import tkinter as tk
from tkinter import font as tkfont, ttk
from pykrx import stock
import threading
import time
import logging
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timedelta
from dataclasses import dataclass
import matplotlib
from collections import deque
import platform
import signal
import sys
import os

# --- 로깅 설정 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# --- Headless/GUI 환경 감지 및 Matplotlib 백엔드 설정 (pyplot 임포트 이전) ---
def _is_headless_env() -> bool:
    """간단한 헤드리스 환경 감지. --nogui/--headless 플래그 또는 DISPLAY 부재 시 True."""
    cli_nogui = any(arg in sys.argv for arg in ("--nogui", "--headless"))
    if cli_nogui:
        return True
    system = platform.system()
    if system == 'Linux':
        if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY') or os.environ.get('MIR_SOCKET')):
            return True
    return False

HEADLESS = _is_headless_env()

try:
    desired_backend = 'Agg' if HEADLESS else 'TkAgg'
    current_backend = (matplotlib.get_backend() or '').lower()
    if current_backend != desired_backend.lower():
        matplotlib.use(desired_backend, force=True)
        logger.info(f"Matplotlib backend switched to {desired_backend}")
    else:
        logger.info(f"Matplotlib backend: {desired_backend}")
except Exception as e:
    logger.warning(f"Matplotlib backend 설정 실패: {e}")

# 이제 백엔드가 설정되었으므로 서브모듈 임포트
if not HEADLESS:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
else:
    FigureCanvasTkAgg = None  # type: ignore
from matplotlib.figure import Figure
import matplotlib.dates as mdates
import matplotlib.font_manager as fm

# --- 한글 폰트 설정 ---
def setup_korean_font():
    """
    시스템에서 사용 가능한 한글 폰트를 찾아서 matplotlib에 설정합니다.
    """
    system = platform.system()
    
    # 우선순위 순서로 폰트 리스트
    if system == 'Windows':
        font_list = ['Malgun Gothic', 'NanumGothic', 'NanumBarunGothic', 'Gulim', 'Dotum']
    elif system == 'Darwin':  # macOS
        font_list = ['AppleGothic', 'NanumGothic', 'Arial Unicode MS']
    else:  # Linux
        font_list = ['NanumGothic', 'NanumBarunGothic', 'UnDotum', 'DejaVu Sans']
    
    # 사용 가능한 폰트 찾기
    available_fonts = [f.name for f in fm.fontManager.ttflist]
    
    for font_name in font_list:
        if font_name in available_fonts:
            matplotlib.rcParams['font.family'] = font_name
            matplotlib.rcParams['axes.unicode_minus'] = False  # 마이너스 기호 깨짐 방지
            logger.info(f"한글 폰트 설정 완료: {font_name}")
            return font_name
    
    # 기본 폰트 사용
    logger.warning("한글 폰트를 찾을 수 없습니다. 기본 폰트를 사용합니다.")
    matplotlib.rcParams['axes.unicode_minus'] = False
    return None

# 폰트 설정 실행
KOREAN_FONT = setup_korean_font()


# --- 설정 클래스 ---
@dataclass
class Config:
    """애플리케이션 설정을 관리하는 클래스"""
    # 모니터링할 주식 종목 코드 (한국 주식 시장)
    # 종목 코드는 6자리 숫자입니다
    # 예: 삼성전자(005930), SK하이닉스(000660), NAVER(035420), 카카오(035720)
    TICKERS: Dict[str, str] = None
    UPDATE_INTERVAL_SECONDS: int = 5  # 데이터 갱신 주기 (초) - 너무 짧으면 API 부하
    MAX_RETRY_ATTEMPTS: int = 3  # 최대 재시도 횟수
    RETRY_DELAY_SECONDS: int = 2  # 재시도 대기 시간 (초)
    
    def __post_init__(self):
        if self.TICKERS is None:
            self.TICKERS = {
                "삼성전자": "005930",
                "SK하이닉스": "000660",
                "NAVER": "035420"
            }


# --- 주식 데이터 관리 클래스 ---
class StockData:
    """
    주식 데이터를 관리하고 yfinance API로부터 데이터를 가져옵니다.
    
    스레드 안전성을 보장하며, 주기적으로 주식 데이터를 업데이트합니다.
    """
    
    def __init__(self, tickers: Dict[str, str], config: Config):
        """
        Args:
            tickers: 종목명과 티커 심볼의 딕셔너리
            config: 설정 객체
        """
        self.tickers = tickers
        self.config = config
        self.data: Dict[str, Dict[str, Any]] = {
            name: {
                "price": "로딩 중...", 
                "change": "", 
                "color": "black",
                "last_update": None,
                "price_value": 0  # 숫자 값 저장
            } 
            for name in tickers.keys()
        }
        # 가격 히스토리 저장 (장 시작부터 종료까지: 5분봉 기준 약 80개)
        self.price_history: Dict[str, deque] = {
            name: deque(maxlen=100) for name in tickers.keys()
        }
        self.time_history: Dict[str, deque] = {
            name: deque(maxlen=100) for name in tickers.keys()
        }
        self._lock = threading.Lock()  # 스레드 안전성을 위한 Lock
        logger.info(f"StockData 초기화 완료: {len(tickers)}개 종목 모니터링")

    def get_data(self, name: str) -> Dict[str, Any]:
        """
        스레드 안전하게 주식 데이터를 가져옵니다.
        
        Args:
            name: 종목명
            
        Returns:
            해당 종목의 데이터 딕셔너리
        """
        with self._lock:
            return self.data.get(name, {}).copy()

    def get_all_data(self) -> Dict[str, Dict[str, Any]]:
        """
        스레드 안전하게 모든 주식 데이터를 가져옵니다.
        
        Returns:
            모든 종목의 데이터 딕셔너리
        """
        with self._lock:
            return {name: data.copy() for name, data in self.data.items()}
    
    def get_price_history(self, name: str) -> Tuple[List, List]:
        """
        스레드 안전하게 가격 히스토리를 가져옵니다.
        
        Args:
            name: 종목명
            
        Returns:
            (시간 리스트, 가격 리스트) 튜플
        """
        with self._lock:
            if name in self.time_history and name in self.price_history:
                return list(self.time_history[name]), list(self.price_history[name])
            return [], []

    def _get_trading_dates(self) -> Tuple[str, str]:
        """
        거래일 날짜를 가져옵니다 (오늘과 최근 거래일).
        
        Returns:
            (시작일, 종료일) 튜플 (YYYYMMDD 형식)
        """
        today = datetime.now()
        
        # 최근 10일간의 날짜로 조회 (주말/공휴일 고려)
        start_date = (today - timedelta(days=10)).strftime("%Y%m%d")
        end_date = today.strftime("%Y%m%d")
        
        return start_date, end_date

    def _is_market_open(self) -> bool:
        """
        현재 장이 열려있는지 확인합니다.
        
        Returns:
            장 시간 여부
        """
        now = datetime.now()
        # 평일(월~금) 체크
        if now.weekday() >= 5:  # 토요일(5), 일요일(6)
            return False
        
        # 장 시간: 09:00 ~ 15:30
        market_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        
        return market_open <= now <= market_close
    
    def _add_to_history(self, name: str, price: int) -> None:
        """
        가격 히스토리에 데이터를 추가합니다.
        
        Args:
            name: 종목명
            price: 가격
        """
        with self._lock:
            current_time = datetime.now()
            self.price_history[name].append(price)
            self.time_history[name].append(current_time)

    def _fetch_stock_info(self, name: str, ticker_code: str) -> Optional[Dict[str, Any]]:
        """
        단일 주식의 정보를 가져옵니다 (pykrx 사용).
        
        Args:
            name: 종목명
            ticker_code: 종목 코드 (6자리 숫자)
            
        Returns:
            성공 시 주식 데이터 딕셔너리, 실패 시 None
        """
        for attempt in range(self.config.MAX_RETRY_ATTEMPTS):
            try:
                # 날짜 범위 가져오기
                start_date, end_date = self._get_trading_dates()
                
                # pykrx로 OHLCV 데이터 가져오기
                df = stock.get_market_ohlcv_by_date(start_date, end_date, ticker_code)
                
                if df is not None and not df.empty and len(df) >= 2:
                    # 최근 2개 거래일 데이터
                    previous_close = int(df['종가'].iloc[-2])
                    current_price = int(df['종가'].iloc[-1])
                    change = current_price - previous_close
                    change_percent = (change / previous_close) * 100

                    # 색상 결정 (상승: 빨강, 하락: 파랑, 보합: 검정)
                    if change > 0:
                        color = "red"
                    elif change < 0:
                        color = "blue"
                    else:
                        color = "black"
                    
                    logger.debug(f"{name}({ticker_code}) 데이터 가져오기 성공: {current_price:,}원")
                    return {
                        "price": f"{current_price:,}원",
                        "change": f"{change:+,}원 ({change_percent:+.2f}%)",
                        "color": color,
                        "last_update": datetime.now(),
                        "price_value": current_price  # 숫자 값 저장
                    }
                    
                elif df is not None and not df.empty and len(df) == 1:
                    # 데이터가 1일치만 있는 경우 (주말, 공휴일 등)
                    current_price = int(df['종가'].iloc[-1])
                    logger.warning(f"{name}({ticker_code}): 전일 데이터 없음, 현재가만 표시")
                    return {
                        "price": f"{current_price:,}원",
                        "change": "전일 데이터 없음",
                        "color": "gray",
                        "last_update": datetime.now(),
                        "price_value": current_price
                    }
                else:
                    logger.warning(f"{name}({ticker_code}): 빈 데이터 수신 - 장 마감 전이거나 휴장일")
                    
            except Exception as e:
                logger.error(f"{name}({ticker_code}) 데이터 가져오기 실패 (시도 {attempt + 1}/{self.config.MAX_RETRY_ATTEMPTS}): {e}")
                if attempt < self.config.MAX_RETRY_ATTEMPTS - 1:
                    time.sleep(self.config.RETRY_DELAY_SECONDS)
                    
        return None

    def fetch_data(self) -> None:
        """
        백그라운드에서 주기적으로 주식 데이터를 가져옵니다.
        
        이 메서드는 무한 루프로 실행되며, daemon 스레드에서 동작합니다.
        실시간으로 가격 데이터를 축적하여 차트를 그립니다.
        """
        logger.info("주식 데이터 가져오기 시작 (pykrx 사용)")
        
        # 장 시작 여부 확인
        if self._is_market_open():
            logger.info("장 시간입니다. 실시간 데이터 수집을 시작합니다.")
        else:
            logger.info("장 시간이 아닙니다. 마지막 종가 데이터를 표시합니다.")
        
        # 주기적 업데이트
        while True:
            try:
                # 현재 가격 정보 업데이트
                for name, ticker_code in self.tickers.items():
                    try:
                        result = self._fetch_stock_info(name, ticker_code)
                        
                        with self._lock:
                            if result:
                                self.data[name] = result
                                
                                # 가격 히스토리에 추가 (유효한 가격이 있을 때만)
                                if "price_value" in result and result["price_value"] > 0:
                                    self._add_to_history(name, result["price_value"])
                                
                            else:
                                # 모든 재시도 실패 시
                                self.data[name] = {
                                    "price": "데이터 없음",
                                    "change": "장 마감 또는 휴장일",
                                    "color": "gray",
                                    "last_update": None,
                                    "price_value": 0
                                }
                    except Exception as e:
                        logger.debug(f"{name} 데이터 수집 중 오류: {e}")
                
                logger.debug(f"데이터 업데이트 완료, {self.config.UPDATE_INTERVAL_SECONDS}초 후 재시도")
                time.sleep(self.config.UPDATE_INTERVAL_SECONDS)
                
            except Exception as e:
                logger.error(f"데이터 수집 스레드 오류: {e}")
                time.sleep(self.config.UPDATE_INTERVAL_SECONDS)

# --- GUI 애플리케이션 클래스 ---
class StockMonitorApp(tk.Tk):
    """
    Tkinter를 사용한 GUI 애플리케이션 클래스입니다.
    
    실시간으로 주식 데이터를 화면에 표시하고 업데이트합니다.
    """
    
    # 색상 테마
    COLORS = {
        'bg_dark': '#0a0e27',        # 어두운 배경
        'bg_card': '#1a1f3a',        # 카드 배경
        'bg_hover': '#252b4a',       # 호버 배경
        'text_primary': '#ffffff',   # 주요 텍스트
        'text_secondary': '#8b93b8', # 보조 텍스트
        'accent': '#667eea',         # 강조 색상
        'up': '#00d4aa',            # 상승 (녹색)
        'down': '#ff6b9d',          # 하락 (분홍)
        'neutral': '#667eea',        # 보합 (파랑)
        'border': '#2d3458',         # 테두리
    }
    
    def __init__(self, stock_data: StockData, config: Config):
        """
        Args:
            stock_data: 주식 데이터 관리 객체
            config: 설정 객체
        """
        if HEADLESS:
            raise RuntimeError("GUI 초기화가 요청되었지만 현재는 headless 모드입니다.")
        super().__init__()
        self.stock_data = stock_data
        self.config = config
        self.is_fullscreen = False  # 전체화면 상태 추적
        
        self.title("📈 한국 주식 모니터 - KRX")
        self.configure(bg=self.COLORS['bg_dark'])
        
        # 창 종료 시 정리 작업
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        # 폰트 설정 (시스템 폰트 사용)
        font_family = KOREAN_FONT if KOREAN_FONT else "Arial"
        logger.info("Tk 폰트 생성 시작")
        self.title_font = tkfont.Font(family=font_family, size=16, weight="bold")
        self.stock_name_font = tkfont.Font(family=font_family, size=14, weight="bold")
        self.price_font = tkfont.Font(family=font_family, size=32, weight="bold")
        self.change_font = tkfont.Font(family=font_family, size=14)
        self.small_font = tkfont.Font(family=font_family, size=9)
        logger.info("Tk 폰트 생성 완료")

        # UI 요소들을 담을 딕셔너리
        self.labels: Dict[str, Dict[str, Any]] = {}
        self.stock_cards: Dict[str, tk.Frame] = {}
        self.last_chart_update_time: Dict[str, float] = {}  # 차트 마지막 업데이트 시간
        
        logger.info("위젯 생성 시작")
        self.create_widgets()
        logger.info("위젯 생성 완료")

        # 초기 렌더 강제 (표시 문제 방지)
        try:
            self.update_idletasks()
            self.update()
        except Exception:
            pass

        # 창을 전면에 표시 (모든 위젯 생성 후)
        self.after(100, self._bring_to_front)
        
        # GUI 업데이트 시작 (모든 위젯이 준비된 후)
        self.after(500, self.update_gui)
        
        # 전체 화면으로 시작
        self.after(200, self._toggle_fullscreen)

        logger.info("GUI 애플리케이션 초기화 완료")

    def create_widgets(self) -> None:
        """화면에 표시될 위젯들을 생성합니다."""
        # 헤더
        header = tk.Frame(self, bg=self.COLORS['bg_dark'], height=60)
        header.pack(side=tk.TOP, fill=tk.X, padx=20, pady=(20, 10))
        
        title_label = tk.Label(
            header,
            text="📈 한국 주식 모니터",
            font=self.title_font,
            bg=self.COLORS['bg_dark'],
            fg=self.COLORS['text_primary']
        )
        title_label.pack(side=tk.LEFT)
        
        subtitle_label = tk.Label(
            header,
            text="Korea Exchange (KRX) - 실시간 차트",
            font=self.small_font,
            bg=self.COLORS['bg_dark'],
            fg=self.COLORS['text_secondary']
        )
        subtitle_label.pack(side=tk.LEFT, padx=(10, 0))
        
        # 메인 컨테이너 (스크롤 가능)
        main_container = tk.Frame(self, bg=self.COLORS['bg_dark'])
        main_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=20, pady=(0, 20))
        
        # 스크롤 가능한 프레임
        canvas_scroll = tk.Canvas(main_container, bg=self.COLORS['bg_dark'], highlightthickness=0)
        scrollbar = ttk.Scrollbar(main_container, orient="vertical", command=canvas_scroll.yview)
        scrollable_frame = tk.Frame(canvas_scroll, bg=self.COLORS['bg_dark'])
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas_scroll.configure(scrollregion=canvas_scroll.bbox("all"))
        )
        
        canvas_scroll.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas_scroll.configure(yscrollcommand=scrollbar.set)
        
        canvas_scroll.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # 주식 카드 생성 (차트 포함)
        for name in self.stock_data.tickers.keys():
            self._create_stock_card_with_chart(scrollable_frame, name)
    
    def _create_stock_card_with_chart(self, parent: tk.Frame, name: str) -> None:
        """차트가 포함된 주식 카드를 생성합니다."""
        # 카드 프레임
        card = tk.Frame(
            parent,
            bg=self.COLORS['bg_card'],
            highlightbackground=self.COLORS['border'],
            highlightthickness=2
        )
        card.pack(fill=tk.X, pady=(0, 15), padx=5)
        
        # 내부 패딩
        inner_frame = tk.Frame(card, bg=self.COLORS['bg_card'])
        inner_frame.pack(fill=tk.BOTH, padx=15, pady=15)
        
        # 왼쪽: 정보 영역
        left_frame = tk.Frame(inner_frame, bg=self.COLORS['bg_card'])
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH)
        
        # 종목명
        name_label = tk.Label(
            left_frame,
            text=name,
            font=self.stock_name_font,
            bg=self.COLORS['bg_card'],
            fg=self.COLORS['text_primary']
        )
        name_label.pack(anchor='w')
        
        # 가격
        price_label = tk.Label(
            left_frame,
            text="로딩 중...",
            font=self.price_font,
            bg=self.COLORS['bg_card'],
            fg=self.COLORS['text_primary']
        )
        price_label.pack(anchor='w', pady=(5, 0))
        
        # 등락률
        change_label = tk.Label(
            left_frame,
            text="",
            font=self.change_font,
            bg=self.COLORS['bg_card'],
            fg=self.COLORS['text_secondary']
        )
        change_label.pack(anchor='w', pady=(2, 0))
        
        # 업데이트 시간
        time_label = tk.Label(
            left_frame,
            text="",
            font=self.small_font,
            bg=self.COLORS['bg_card'],
            fg=self.COLORS['text_secondary']
        )
        time_label.pack(anchor='w', pady=(5, 0))
        
        # 오른쪽: 차트 영역 (플레이스홀더)
        chart_frame = tk.Frame(inner_frame, bg=self.COLORS['bg_card'], width=320, height=160)
        chart_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(20, 0))
        chart_frame.pack_propagate(False)
        
        # 로딩 중 메시지
        loading_label = tk.Label(
            chart_frame,
            text="차트 로딩 중...",
            font=self.small_font,
            bg=self.COLORS['bg_card'],
            fg=self.COLORS['text_secondary']
        )
        loading_label.pack(expand=True)
        
        # 저장
        self.stock_cards[name] = card
        self.labels[name] = {
            "card": card,
            "price": price_label,
            "change": change_label,
            "time": time_label,
            "figure": None,
            "ax": None,
            "canvas": None,
            "chart_frame": chart_frame,
            "chart_created": False,
            "loading_label": loading_label
        }
        
        # 차트는 나중에 생성하도록 스케줄링 (UI 표시 후)
        self.after(1000 + len(self.labels) * 200, lambda n=name: self._create_chart_delayed(n))
    
    def _create_chart_delayed(self, name: str) -> None:
        """차트를 지연하여 생성합니다 (lazy loading)."""
        if name not in self.labels:
            return
        
        label_data = self.labels[name]
        if label_data.get("chart_created", False):
            return
        
        chart_frame = label_data.get("chart_frame")
        if chart_frame is None:
            return
        
        try:
            logger.info(f"차트 생성 시작: {name}")
            
            # 기존 로딩 라벨 제거
            if label_data.get("loading_label"):
                label_data["loading_label"].destroy()
            
            # 새로운 프레임에 차트 생성
            fig = Figure(figsize=(4, 2), facecolor=self.COLORS['bg_card'], dpi=80)
            ax = fig.add_subplot(111)
            ax.set_facecolor(self.COLORS['bg_card'])
            
            # 축 숨기기
            ax.set_xticks([])
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_visible(False)
            
            # 캔버스 생성 및 표시
            canvas = FigureCanvasTkAgg(fig, chart_frame)
            canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
            
            logger.info(f"차트 생성 완료: {name}")
            
            # 데이터 업데이트
            self.labels[name]["figure"] = fig
            self.labels[name]["ax"] = ax
            self.labels[name]["canvas"] = canvas
            self.labels[name]["chart_created"] = True
            
        except Exception as e:
            logger.error(f"{name} 차트 생성 실패: {e}")
            self.labels[name]["chart_created"] = False
    
    def _update_mini_chart(self, name: str) -> None:
        """개별 미니 차트를 업데이트합니다."""
        if name not in self.labels:
            return
        
        label_data = self.labels[name]
        if not label_data.get("chart_created", False) or "ax" not in label_data or label_data["ax"] is None:
            return
        
        # 차트 업데이트 throttling (2초마다만 업데이트)
        import time
        current_time = time.time()
        if name in self.last_chart_update_time:
            if current_time - self.last_chart_update_time[name] < 2.0:
                return
        
        try:
            ax = label_data["ax"]
            canvas = label_data["canvas"]
            
            if canvas is None:
                return
            
            # 히스토리 데이터 가져오기
            times, prices = self.stock_data.get_price_history(name)
            
            # 데이터가 없거나 변경 없으면 업데이트 안 함
            if len(times) == 0 or len(prices) == 0:
                return
            
            # 차트 클리어
            ax.clear()
            ax.set_facecolor(self.COLORS['bg_card'])
            
            # 데이터 가져오기
            data = self.stock_data.get_data(name)
            color = self.COLORS['up'] if data.get('color') == 'red' else \
                    self.COLORS['down'] if data.get('color') == 'blue' else \
                    self.COLORS['neutral']
            
            # 라인 차트 그리기 (심플한 버전)
            ax.plot(times, prices, color=color, linewidth=2, alpha=0.8)
            
            # 영역 채우기
            ax.fill_between(range(len(prices)), prices, alpha=0.15, color=color)
            
            # 축 및 테두리 숨기기
            ax.set_xticks([])
            ax.set_yticks([])
            for spine in ax.spines.values():
                spine.set_visible(False)
            
            # 여백 최소화
            if label_data.get("figure"):
                label_data["figure"].tight_layout(pad=0)
            
            # 캔버스 업데이트 (비동기)
            canvas.draw_idle()
            self.last_chart_update_time[name] = current_time
            
        except Exception as e:
            logger.debug(f"{name} 차트 업데이트 중 오류 발생: {e}")
    
    def update_gui(self) -> None:
        """
        stock_data 객체의 데이터를 GUI 라벨에 업데이트합니다.
        
        스레드 안전한 방식으로 데이터를 가져와 화면을 갱신합니다.
        """
        try:
            # 스레드 안전하게 데이터 가져오기
            all_data = self.stock_data.get_all_data()
            
            for name, data in all_data.items():
                if name in self.labels:
                    # 색상 매핑
                    original_color = data.get("color", "black")
                    if original_color == "red":
                        display_color = self.COLORS['up']
                    elif original_color == "blue":
                        display_color = self.COLORS['down']
                    else:
                        display_color = self.COLORS['neutral']
                    
                    # 가격 업데이트
                    self.labels[name]["price"].config(
                        text=data.get("price", "N/A"), 
                        fg=display_color
                    )
                    
                    # 등락률 업데이트
                    self.labels[name]["change"].config(
                        text=data.get("change", ""), 
                        fg=display_color
                    )
                    
                    # 마지막 업데이트 시간 표시
                    last_update = data.get("last_update")
                    if last_update:
                        time_str = last_update.strftime("%H:%M:%S")
                        self.labels[name]["time"].config(text=f"🕐 {time_str}")
                    else:
                        self.labels[name]["time"].config(text="")
                    
                    # 미니 차트 업데이트 (throttled)
                    self._update_mini_chart(name)
        
        except Exception as e:
            logger.debug(f"GUI 업데이트 중 오류 발생: {e}")
        
        # 2초마다 GUI 업데이트 함수 재호출 (UI 반응성을 위해 빈도 감소)
        self.after(2000, self.update_gui)
    
    def _on_closing(self) -> None:
        """창 종료 시 호출되는 정리 작업"""
        logger.info("애플리케이션 종료 중...")
        self.destroy()

    def _bring_to_front(self) -> None:
        """윈도우를 전면으로 가져와 초기 표시 문제를 방지합니다."""
        try:
            self.deiconify()
            self.lift()
            self.attributes('-topmost', True)
            self.after(200, lambda: self.attributes('-topmost', False))
            # 가시성 진단 로그
            is_mapped = self.winfo_ismapped()
            logger.info(f"윈도우 표시 상태: mapped={is_mapped}, geometry={self.geometry()}")
            
            # Windows에서 추가 처리
            import sys
            if sys.platform == 'win32':
                try:
                    import ctypes
                    # 윈도우를 전면으로
                    hwnd = ctypes.windll.kernel32.GetForegroundWindow()
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                except:
                    pass
        except Exception as e:
            logger.warning(f"윈도우 포커스 설정 실패: {e}")

    def _toggle_fullscreen(self) -> None:
        """전체 화면 모드를 토글합니다."""
        if self.is_fullscreen:
            self.attributes('-fullscreen', False)
            self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")
            self.is_fullscreen = False
            logger.info("전체 화면 모드 해제")
        else:
            self.attributes('-fullscreen', True)
            self.is_fullscreen = True
            logger.info("전체 화면 모드 설정")


# --- 메인 실행 부분 ---
def main() -> None:
    """애플리케이션의 메인 진입점"""
    # 설정 초기화
    config = Config()
    
    logger.info("=" * 50)
    logger.info("한국 주식 모니터 애플리케이션 시작 (pykrx 사용)")
    logger.info(f"모니터링 종목: {', '.join(config.TICKERS.keys())}")
    logger.info(f"종목 코드: {', '.join(config.TICKERS.values())}")
    logger.info(f"업데이트 주기: {config.UPDATE_INTERVAL_SECONDS}초")
    logger.info("=" * 50)
    
    # 주식 데이터 관리자 초기화
    stock_data_manager = StockData(config.TICKERS, config)

    # 데이터 가져오기를 백그라운드 스레드에서 실행
    fetch_thread = threading.Thread(
        target=stock_data_manager.fetch_data, 
        daemon=True,
        name="StockDataFetchThread"
    )
    fetch_thread.start()
    logger.info("백그라운드 데이터 수집 스레드 시작")

    # SIGINT 처리: Ctrl+C 동작 보장
    def handle_sigint(signum, frame):
        logger.info("SIGINT 수신: 애플리케이션 종료 시도")
        if not HEADLESS:
            try:
                tk._default_root and tk._default_root.quit()
            except Exception:
                pass
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, handle_sigint)
    except Exception as e:
        logger.warning(f"SIGINT 핸들러 등록 실패: {e}")

    # 실행 모드 분기
    if HEADLESS:
        logger.info("Headless 모드 감지: GUI 없이 콘솔 출력으로 실행합니다. (--nogui로 강제 가능)")
        try:
            while True:
                all_data = stock_data_manager.get_all_data()
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 실시간 종목 현황")
                for name, data in all_data.items():
                    price = data.get('price', 'N/A')
                    change = data.get('change', '')
                    print(f"- {name}: {price} {change}")
                print("")
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt: 애플리케이션 종료")
        except Exception as e:
            logger.error(f"헤드리스 모드 실행 중 오류: {e}", exc_info=True)
        finally:
            logger.info("애플리케이션 종료 완료")
        return
    
    # GUI 애플리케이션 실행
    logger.info("GUI 초기화 시작")
    app = StockMonitorApp(stock_data_manager, config)
    logger.info("GUI 생성 완료, mainloop 시작")
    logger.info(f"Tk root: {app}, mapped={app.winfo_ismapped()}, geometry={app.geometry()}")
    try:
        # ESC와 Ctrl+Q 단축키로 종료
        app.bind('<Escape>', lambda e: app._toggle_fullscreen())
        app.bind('<Control-q>', lambda e: app._on_closing())

        # 윈도우 표시 강제
        app.deiconify()
        app.attributes('-topmost', True)
        app.after(300, lambda: app.attributes('-topmost', False))
        
        logger.info("GUI mainloop 시작...")
        app.mainloop()
    except Exception as e:
        logger.error(f"애플리케이션 실행 중 오류 발생: {e}", exc_info=True)
    finally:
        logger.info("애플리케이션 종료 완료")


if __name__ == "__main__":
    main()