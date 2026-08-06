import os

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CACHE_DIR = os.path.join(DATA_DIR, "cache")

os.makedirs(CACHE_DIR, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

NSE_BASE_URL = "https://www.nseindia.com"
NSE_HOMEPAGE = NSE_BASE_URL
NSE_FII_DII_API = f"{NSE_BASE_URL}/api/fiidiiTradeReact"
NSE_OPTION_CHAIN_CONTRACT_INFO = f"{NSE_BASE_URL}/api/option-chain-contract-info"
NSE_OPTION_CHAIN_V3 = f"{NSE_BASE_URL}/api/option-chain-v3"
NSE_BLOCK_DEALS_API = f"{NSE_BASE_URL}/api/NextApi/apiClient/egrApi?functionName=getEGRBlockDealData"
NSE_GIFT_NIFTY_API = f"{NSE_BASE_URL}/api/NextApi/apiClient?functionName=getGiftNifty"
NSE_ALL_INDICES_API = f"{NSE_BASE_URL}/api/allIndices"
NSE_IPO_API = f"{NSE_BASE_URL}/api/ipo-information"

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"

MONEYCONTROL_CALENDAR_URL = (
    "https://www.moneycontrol.com/markets/earnings/results-calendar"
    "?id=All&name=All&activeDate={date}"
)

TICKERS = {
    "brent": "BZ=F",
    "wti": "CL=F",
    "usd_inr": "INR=X",
    "dow": "^DJI",
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "nikkei": "^N225",
    "hang_seng": "^HSI",
    "shanghai": "000001.SS",
    "sgx_nifty": "^NSEI",
    "gift_nifty": "SGXNIFTY.BO",
    "tnx": "^TNX",
    "dxy": "DX-Y.NYB",
}

FACTORS_FILE = os.path.join(os.path.dirname(__file__), "factors.txt")
FACTORS_JSON = os.path.join(DATA_DIR, "factors.json")

AGENT_TIMEOUT = 15
TOTAL_TIMEOUT = 90
RETRY_ATTEMPTS = 2
CACHE_TTL_HOURS = 24
NEWS_CACHE_TTL_HOURS = 1
NSE_RATE_LIMIT = 1.0
