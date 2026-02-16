"""
title: Magyar Modell Router (Egyszerű)
description: Teljes magyar asszisztens - modell választás, magyar válaszok, REASONING megjelenítés, internet keresés
version: 2.0.0

Használat: Admin -> Functions -> Create -> töltsd fel ezt a fájlt

Környezeti változók:
- OLLAMA_IP, OLLAMA_PORT: Ollama szerver
- SEARXNG_URL: Internet keresés (pl. http://10.0.0.45:8888)
- JUPYTER_URL: Jupyter szerver (hibakereséshez, pl. http://10.0.0.80:8888)
- JUPYTER_TOKEN: Jupyter hitelesítési token (opcionális)
- CODE_TIMEOUT_SEC: Max kód futtatási idő mp-ben (default: 30) – állítsd az Open WebUI timeoutjával egyezőre (pl. 60)
- OLLAMA_TIMEOUT_SEC: Ollama válasz timeout mp-ben (default: 1800 = 30 min) – qwen3 gondolkodásnál hasznos
- WEATHER_TZ: Időzóna a „ma” dátumhoz (pl. Europe/Budapest, Europe/Vienna). Ha nincs: alapértelmezett Europe/Budapest, ne UTC.
- USER_WEATHER_LOCATION: Tartózkodási hely, ha a mondatban nincs (pl. Oberndorf bei Salzburg, Budapest). Body/__user__: user_location, location, city felülírja.
- USE_IP_WEATHER_LOCATION: 1 = ha nincs hely, ipapi.co alapján IP-ből tartózkodási hely (body['client_ip']/user_ip opcionális). 0 = kikapcsolva. Default 1.
- USE_MELYKERESES: 1/0 – env: mélykeresés alapértelmezett. Források: !mélykeresés be/ki, body['_melykereses'] (model_router_filter), UserValves, Valves, env
- USE_JINA_READER: 1/0 – egyszerű SearXNG keresésnél a top 3 lap tartalma Jina Readerrel, default 1
- JINA_READER_URL: Jina Reader API alap URL (default: http://10.0.0.239:3000). Cél URL path formátumban: /https://...
  Ha a ReaderApi ERR_INVALID_URL-t ad: a szervernek a path-ot dekódolnia kell (pl. Node: decodeURIComponent(req.path.slice(1))) a normalizeUrl előtt.
- SEARCH_CONTEXT_MAX: Keresés+Jina kontextus max karakter (default: 14000) – túl nagy = lassú/timeout
- JINA_READER_DELAY_SEC: Szünet másodpercben lapok között (default: 2) – ha „Nem sikerült“ sok lapnál, növeld 3-ra
- DEBUG_JINA_READER: 1 = a chatben megjelenik, hívjuk-e a Jinát és miért (kihagyva: mélykeresés/időjárás/kép).
  A ReaderApi „Page N created / Closing page N” log ~30 mp-enként = a Reader belső pool ciklusa (idle lapok
  cseréje), nem feltétlenül a mi kérésünk – ha nincs chat kérés, a router egyáltalán nem hívja a Jinát.

Parancssor: python model_router_simple.py --jupyter [--jupyter-url URL] | --timeout N
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import aiohttp  # pyright: ignore[reportMissingImports]
from pydantic import BaseModel, Field  # pyright: ignore[reportMissingImports]

# Jupyter biztonság: timeout (mp), URL, token
CODE_TIMEOUT_SEC = int(os.environ.get("CODE_TIMEOUT_SEC", "30"))
OLLAMA_TIMEOUT_SEC = int(os.environ.get("OLLAMA_TIMEOUT_SEC", "1800"))  # 30 min – qwen3/cogito gondolkodás
JUPYTER_URL = os.environ.get("JUPYTER_URL", "").strip() or None
JUPYTER_TOKEN = os.environ.get("JUPYTER_TOKEN", "").strip() or None


def _run_code_with_timeout(code: str, timeout: int = None) -> Tuple[bool, str, int]:
    """Python kód futtatása max időkéréssel. Vissza: (siker, stdout+stderr, kilépési kód)."""
    timeout = timeout or CODE_TIMEOUT_SEC
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name
    try:
        result = subprocess.run(
            [sys.executable, path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, out.strip() or "(nincs kimenet)", result.returncode
    except subprocess.TimeoutExpired:
        return False, f"⏱️ Időtúllépés: a kód {timeout} másodpercnél tovább futott.", -1
    except Exception as e:
        return False, f"Hiba: {e}", -1
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def _check_jupyter_reachable(url: str, token: str = None) -> Tuple[bool, str]:
    """Ellenőrzi, hogy a Jupyter szerver elérhető-e. Vissza: (elérhető, üzenet)."""
    url = url.rstrip("/")
    token = token or JUPYTER_TOKEN
    suf = f"?token={urllib.parse.quote(token)}" if token else ""
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for endpoint in ["/api/status", "/"]:
                try:
                    async with session.get(f"{url}{endpoint}{suf}") as resp:
                        if resp.status < 500:
                            return True, f"✓ Jupyter elérhető: {url} (HTTP {resp.status})"
                except Exception:
                    continue
        return False, f"✗ Jupyter nem válaszol: {url}"
    except asyncio.TimeoutError:
        return False, f"✗ Időtúllépés: {url} nem érhető el 10 s alatt."
    except Exception as e:
        return False, f"✗ Hiba: {e}"

# Magyar hét napjai
_WEEKDAYS = ("hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap")


def _parse_melykereses_command(msg: str) -> Tuple[str, Optional[bool]]:
    """Chat parancs: !mélykeresés be / !mélykeresés ki – vissza: (tisztított üzenet, override vagy None)."""
    m = (msg or "").strip()
    if not m:
        return m, None
    # !mélykeresés be / !mélykeresés ki – elején vagy közepén, case-insensitive
    pat_be = re.compile(r"^!mélykeresés\s+be\s*", re.IGNORECASE)
    pat_ki = re.compile(r"^!mélykeresés\s+ki\s*", re.IGNORECASE)
    if pat_be.search(m):
        clean = pat_be.sub("", m).strip()
        return clean if clean else m, True
    if pat_ki.search(m):
        clean = pat_ki.sub("", m).strip()
        return clean if clean else m, False
    # Középen is engedélyezzük
    pat_be_mid = re.compile(r"!mélykeresés\s+be\s*", re.IGNORECASE)
    pat_ki_mid = re.compile(r"!mélykeresés\s+ki\s*", re.IGNORECASE)
    if pat_be_mid.search(m):
        clean = pat_be_mid.sub("", m).strip()
        return clean if clean else m, True
    if pat_ki_mid.search(m):
        clean = pat_ki_mid.sub("", m).strip()
        return clean if clean else m, False
    return m, None

# Biztonsági szűrő: veszélyes kódrészletek blokkolása
_BLOCKED_PLACEHOLDER_HU = "\n\n⚠️ [BLOKKOLVA – biztonsági okok miatt: veszélyes vagy etikailag kétértelmű kód]\n\n"
_BLOCKED_PLACEHOLDER_EN = "\n\n⚠️ [BLOCKED – for security reasons: dangerous or ethically ambiguous code]\n\n"
_BLOCKED_PLACEHOLDER_DE = "\n\n⚠️ [GESPERRT – aus Sicherheitsgründen: gefährlicher oder ethisch fragwürdiger Code]\n\n"


def _detect_user_language(user_message: str) -> str:
    """Felhasználó üzenetének nyelve: hu, en, de, vagy 'en' alapértelmezett."""
    msg = (user_message or "").strip().lower()
    # Magyar: áéíóöőúüű vagy gyakori magyar szavak
    hu_chars = len(re.findall(r"[áéíóöőúüű]", msg))
    hu_words = ["egy", "hogy", "vagy", "kell", "kellene", "szeretnék", "kérem", "tudnál", "van", "lesz"]
    if hu_chars >= 2 or any(w in msg for w in hu_words):
        return "hu"
    # Német: äöü ß
    de_chars = len(re.findall(r"[äöüß]", msg))
    de_words = ["bitte", "könnten", "kann", "haben", "danke", "code", "script"]
    if de_chars >= 2 or any(w in msg for w in de_words):
        return "de"
    return "en"

_DANGEROUS_PATTERNS = [
    # Leállítás, újraindítás, rendszer
    r"\bshutdown\b", r"\breboot\b", r"\bpoweroff\b", r"\bhalt\b", r"\binit\s+[016]\b",
    r"subprocess\.(run|call|Popen)\s*\([^)]*shutdown", r"os\.system\s*\(\s*['\"]\s*shutdown",
    r"subprocess\.(run|call|Popen)\s*\([^)]*reboot", r"os\.system\s*\(\s*['\"]\s*reboot",
    r"systemctl\s+(poweroff|reboot|halt)",
    # Fájlrendszer – törlés, formázás, írás rendszerhez
    r"rm\s+-[rf]\s", r"rm\s+-\w*rf", r"rmdir\s+/", r"del\s+/[sf]\s",
    r"format\s+[c-z]:", r"mkfs\.", r"dd\s+if=.*of=/dev/", r">\s*/dev/sd",
    r"rm\s+-rf\s+/", r"Remove-Item\s+.*-Recurse\s+-Force",
    # Letöltés / feltöltés / hálózati lekérés
    r"curl\s+.*-o\s", r"wget\s+", r"requests\.(get|post)\s*\(", r"urllib\.request\.(urlopen|urlretrieve)",
    r"ftp\.", r"scp\s+", r"\.download\s*\(", r"\.upload\s*\(", r"HttpClient\s*\.", r"WebClient\s*\.",
    r"Invoke-WebRequest", r"Invoke-RestMethod", r"Start-BitsTransfer",
    # Rendszer / shell / tetszőleges parancs
    r"os\.system\s*\(", r"subprocess\.(run|call|Popen)\s*\(", r"eval\s*\(", r"exec\s*\(",
    r"__import__\s*\(", r"compile\s*\(", r"execfile\s*\(", r"shell\s*=\s*True",
    r"Process\.Start", r"Runtime\.getRuntime\s*\(\)\.exec",
    # Processz / kill
    r"kill\s+-9", r"pkill\s+", r"taskkill\s+", r"os\.kill\s*\(", r"Process\.Kill",
    # Adatbázis – pusztító
    r"DROP\s+TABLE", r"TRUNCATE\s+", r"DROP\s+DATABASE", r"DELETE\s+FROM\s+\w+\s*;\s*$",
    # Jelszó / titkos adat / keylogger
    r"getpass|keylogger|keystroke|read_password",
    r"api_key\s*=\s*['\"][^'\"]+['\"]", r"secret\s*=\s*['\"][^'\"]+['\"]",
    r"password\s*=\s*['\"][^'\"]+['\"]", r"\.env\s*.*password",
    # Hálózat – nyers socket, sniffing, reverse shell
    r"socket\.socket\s*\(", r"raw\s+socket", r"scapy|pyshark|sniff\s*\(",
    r"reverse_shell|bind_shell|nc\s+-e\s+/bin", r"ncat.*-e",
    # Jogosultság / setuid / setgid
    r"chmod\s+[0-7]{3,4}", r"chmod\s+\+[sx]", r"setuid\s*\(", r"setgid\s*\(",
    r"chown\s+root", r"sudo\s+chmod",
    # Kártevő / kripto / ransomware
    r"cryptominer|mining\.", r"ransomware|encrypt\s+files", r"encrypt\s+directory",
    # Clipboard / képernyő / adat kinyerés
    r"pyperclip|clipboard\.get", r"ImageGrab\.grab", r"mss\.mss", r"screenshot\s*\(",
    # Windows / .NET veszélyes
    r"Base64\.Decode", r"Invoke-Expression", r"System\.Diagnostics\.Process\.Start",
    r"File\.Delete\s*\(", r"Directory\.Delete\s*\(", r"\.WriteAllText\s*\([^)]*C:\\",
    # Etikailag kétértelmű / szürkezóna
    r"browser_cookie|selenium.*cookie", r"steal\s+cookie", r"\.getCookies\s*\(",
    r"keylog|screen\s+capture\s+without", r"webcam\s+capture",
    # Memória terhelés / DoS – végtelen hurok, memória feltöltés
    r"while\s+True\s*:[\s\S]{0,200}\.append\s*\(", r"for\s+_\s+in\s+iter\s*\(\s*int\s*,\s*1\s*\)",
    r"10\s*\*\s*\*\s*[0-9]{6,}", r"\[\s*0\s*\]\s*\*\s*[0-9]{6,}",
    r"bytearray\s*\(\s*[0-9]{7,}", r"b['\"]\s*\*\s*[0-9]{7,}",
    r"range\s*\(\s*[0-9]{7,}\s*\)", r"malloc\s*\(\s*[0-9]{7,}",
    # Memória piszkálás – alacsony szintű hozzáférés
    r"ctypes\.(memmove|memset|memcpy)\s*\(",
    r"mmap\.mmap\s*\(",
    r"BufferOverflow|buffer\s*overflow|stack\s*overflow\s*exploit",
]


def _is_dangerous_code(text: str) -> bool:
    """Veszélyes vagy etikailag kétértelmű kódot tartalmaz-e."""
    if not text or len(text.strip()) < 3:
        return False
    t = text.replace(" ", " ").replace("\n", " ")
    for pat in _DANGEROUS_PATTERNS:
        if re.search(pat, t, re.IGNORECASE):
            return True
    return False


def _get_blocked_placeholder(lang: str) -> str:
    """Nyelv szerinti blokkolt üzenet."""
    if lang == "hu":
        return _BLOCKED_PLACEHOLDER_HU
    if lang == "de":
        return _BLOCKED_PLACEHOLDER_DE
    return _BLOCKED_PLACEHOLDER_EN


def _make_code_non_runnable(code: str) -> str:
    """Kód megjelenítése komment formában – látható, de nem futtatható."""
    lines = code.split("\n")
    commented = []
    for line in lines:
        s = line.rstrip()
        if not s:
            commented.append("#")
        else:
            commented.append("# " + line.rstrip())
    return "\n".join(commented)


def _clean_weather_response(text: str) -> str:
    """Időjárás válasz utófeldolgozás: echo instrukciók és rossz dátum eltávolítása."""
    if not text:
        return text
    date_str, weekday, _ = _get_current_datetime_str()
    tomorrow_str = (_get_now() + timedelta(days=1)).strftime("%Y.%m.%d")
    lines = text.split("\n")
    out = []
    skip_phrases = [
        "KIZÁRVA A VÁLASZBÓL",
        "Ne írd bele az instrukció",
        "add meg a valós értéket!",
        "Másold be az alábbi",
        "Csak az időjárás adatokat",
        "Ha van 🖼️",
        "Markdown: félkövér",
    ]
    for line in lines:
        if any(p in line for p in skip_phrases):
            continue
        fixed = line
        yr = date_str.split(".")[0]
        fixed = re.sub(r"20(?:21|22|23|24)[\-\.](\d{2})[\-\.](\d{2})", rf"{yr}.\1.\2", fixed)
        out.append(fixed)
    return "\n".join(out)


def _filter_dangerous_code_blocks(text: str, user_message: str = "") -> str:
    """Code blockok (```...```) ellenőrzése; veszélyes blokkokat lecseréli: figyelmeztetés + teljes kód (nem futtatható)."""
    if not text:
        return text
    lang = _detect_user_language(user_message)
    placeholder = _get_blocked_placeholder(lang)
    def replace_block(match: re.Match) -> str:
        full = match.group(0)
        code = (match.group(2) or "").strip()
        if _is_dangerous_code(code):
            non_runnable = _make_code_non_runnable(code)
            return f"{placeholder}\n\n *** \n\n```\n{non_runnable}\n```\n\n *** \n\n"
        return full
    return re.sub(r"```(\w*)\s*\n?([\s\S]*?)```", replace_block, text, flags=re.IGNORECASE)


def _get_now():
    """Jelenlegi idő. WEATHER_TZ vagy TZ = időzóna (pl. Europe/Budapest, Europe/Vienna). Ha nincs beállítva → Europe/Budapest, hogy a dátum mindig a helyi „ma” legyen."""
    tz_name = (os.environ.get("WEATHER_TZ") or os.environ.get("TZ") or "Europe/Budapest").strip()
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now()


def _get_current_datetime_str() -> Tuple[str, str, str]:
    """Visszaadja (dátum, hétnap, idő) stringeket."""
    now = _get_now()
    return now.strftime("%Y.%m.%d"), _WEEKDAYS[now.weekday()], now.strftime("%H:%M:%S")


# Modellek, melyek támogatják az Ollama think API-t (cogito NEM – saját <think> tageket használ)
_THINKING_MODELS = ("qwen3", "qwen2.5-72b-instruct", "deepseek-r1", "deepseek-v3", "gpt-oss")


class ModelRouter:
    MODEL_FAST = "gemma2:2b"
    MODEL_THINK = "hf.co/RichardErkhov/sambanovasystems_-_SambaLingo-Hungarian-Chat-gguf:Q3_K_L"
    MODEL_REASONING = "qwen3:latest"      # Ollama think API – gondolkodás lenyitható blokkban
    MODEL_REASONING_SMALL = "cogito:3b"   # cogito: system prompt alapú thinking
    MODEL_MEDIUM = "hf.co/RichardErkhov/sambanovasystems_-_SambaLingo-Hungarian-Chat-gguf:Q4_1"
    MODEL_CODE = "qwen2.5-coder:latest"
    MODEL_DEEP = "jobautomation/OpenEuroLLM-Hungarian:latest"
    MODEL_VISION = "llava:latest"

    @classmethod
    async def select_model(cls, user_message: str) -> str:
        """Kiválasztja a modellt az üzenet alapján (aszinkron)."""
        await asyncio.sleep(0)
        msg = (user_message or "").strip().lower()

        if any(w in msg for w in ["szia", "hello", "hi", "helló", "szevasz", "viszlát", "pápá", "bye"]):
            return cls.MODEL_FAST

        # magyarázd / miért / hogyan – ELŐBB mint a kód (pl. "magyarázd el pythonban" → gondolkodás)
        if any(w in msg for w in ["miért", "hogyan", "magyarázd", "magyarázz", "működik", "logika"]):
            return cls.MODEL_REASONING_SMALL if len(msg) < 100 else cls.MODEL_REASONING

        code_keywords = [
            "python", "javascript", "js ", "java ", "c++", "c#", "c sharp",
            "php", "ruby", "go ", "golang", "rust", "swift", "kotlin", "scala",
            "typescript", "html", "css", "sql", "bash", "shell",
            "kód", "kod", "code", "script", "def ", "import ", "function ",
        ]
        if any(w in msg for w in code_keywords):
            if any(w in msg for w in ["egyszerü", "egyszerű", "egyszeru", "simple"]):
                return cls.MODEL_FAST
            # Proxmox/HA shutdown teszt – gemma2 kevésbé restriktív (ä/é eltérés: leällitás stb.)
            msg_norm = msg.replace("ä", "a").replace("ö", "o").replace("ü", "u")
            if any(k in msg_norm for k in ["proxmox", "ha funkció", "ha funkcio", "ha teszt", "jupyter", "tesztelni"]) and any(
                k in msg_norm for k in ["leállít", "leallit", "leállítás", "leallitas", "leáll", "shutdown", "reboot", "újraindít", "ujraindit", "python kod", "python kód"]
            ):
                return cls.MODEL_FAST
            return cls.MODEL_CODE

        time_keywords = [
            "mennyi az idő", "menyi az idő", "pontos idő", "pontos idö",
            "hány óra", "hány ora", "mikor", "mi az idő", "jelenlegi idő",
        ]
        if any(w in msg for w in time_keywords):
            return cls.MODEL_FAST

        date_keywords = [
            "hányadika", "hányadik", "milyen dátum", "mi a dátum", "milyen nap",
            "ma milyen nap", "ma melyik nap", "ma melyik dátum",
            "melyik nap", "melyik hét", "ez a hét", "jövő hét", "következő hét",
            "melyik hónap", "hónap", "honap", "január", "február", "dátum",
        ]
        if any(w in msg for w in date_keywords):
            return cls.MODEL_FAST

        weather_keywords = [
            "időjárás", "idojárás", "milyen idő lesz", "milyen idö lesz",
            "lesz-e eső", "lesz e eső", "lesz eső", "eső", "esö",
            "hőmérséklet", "homerseklet", "hő", "szél", "csapadék",
            "napos", "felhős", "felhos", "ködös", "vihar",
            "ma időjárás", "holnap időjárás", "hétvége időjárás",
            "időjárás ma", "időjárás holnap", "időjárás Budapest",
            "weather", "esni fog", "esni fog-e",
        ]
        if any(w in msg for w in weather_keywords):
            return cls.MODEL_THINK

        return cls.MODEL_FAST


class Pipe:
    """Open WebUI Pipe - teljes magyar asszisztens."""
    type = "pipe"
    id = "magyar_model_router_simple"
    name = "Magyar Modell Router"

    class Valves(BaseModel):
        USE_MELYKERESES: bool = Field(
            default=False,
            description="Admin: mélykeresés alapértelmezetten. Ha UserValves nincs beállítva, ez számít.",
        )

    class UserValves(BaseModel):
        USE_MELYKERESES: bool = Field(
            default=False,
            description="Mélykeresés (Reader) – lassabb, részletesebb. BE = Reader, KI = gyors SearXNG. User toggle.",
        )

    def __init__(self, valves=None):
        self.valves = valves if isinstance(valves, Pipe.Valves) else Pipe.Valves(**(valves or {}))
        self.ollama_ip = os.environ.get("OLLAMA_IP", "10.0.0.78")
        self.ollama_port = os.environ.get("OLLAMA_PORT", "11434")
        self.ollama_base_url = f"http://{self.ollama_ip}:{self.ollama_port}"
        raw = os.environ.get("SEARXNG_URL", "http://10.0.0.45:8888").strip()
        self.searxng_url = raw if raw.startswith("http") else f"http://{raw}"

    def pipes(self):
        return [{"id": self.id, "name": self.name}]

    def _needs_web_search(self, msg: str) -> bool:
        """Megállapítja, szükséges-e internet keresés."""
        m = (msg or "").strip().lower()
        # Programozási/általános ismeret kérdések – NEM kell keresés
        code_kw = ["python", "c++", "javascript", "programozás", "programozási nyelv", "kód", "vélemény"]
        if any(k in m for k in code_kw) and not any(k in m for k in ["keress", "keres", "web", "internet", "friss", "aktuális", "legújabb"]):
            return False
        search_kw = [
            "keress rá", "keress rá a", "keress a weben", "keress az interneten", "keress online",
            "keress információt", "friss információ", "aktuális információ",
            "nézz utána", "nézz rá", "utánanéznél", "rákeresnél", "találsz róla", "mit tudsz róla",
            "információ", "adatok", "most", "jelenleg", "aktuális", "friss hírek", "mai hírek", "legfrissebb", "legújabb",
            "mennyi az ára", "árfolyam", "euró árfolyam", "dollár árfolyam", "bitcoin ár", "részvény ár",
            "eredmény", "meccs", "állás", "tabella", "pontszám",
            "hány óra", "pontos idő", "mai dátum",
            "nyitvatartás", "cím", "telefonszám", "hogyan juthatok el",
            "wikipedia", "ki az a", "mi az a", "mikor történt",
            "mi a legnagyobb", "mi a legmagasabb", "mi a legjobb", "hol van", "hol található",
            "keress egy kepet", "keress kepet", "keress képet", "keress nekem egy kepet", "keress nekem kepet",
            "keress nekem", "keress nekem egy",
            "mutass kepet", "mutass képet", "mutass egy kepet", "mutass egy képet",
            "képet keress", "kepet keress", "képkeresés", "kepkereses",
            "hírek", "hirek", "híreket", "hireket", "mai hír", "news", "foglald össze", "foglalj össze",
            "keressed meg", "keress meg", "keresd meg", "keressd meg", "kérd meg", "keres meg",
            "mikor alapították", "mikor alapítottak", "alapították", "alapítottak", "alapította",
            "története", "tortenete", "történetét",
        ]
        weather_kw = [
            "időjárás", "idojárás", "idojaras", "milyen idő", "milyen ido", "idő lesz", "ido lesz",
            "előrejelzés", "elorejelzes", "forecast", "weather",
            "eső", "esik", "esni fog", "esö", "zápor", "zapor", "hó", "havazás", "havazni fog", "havazik", "ho",
            "havas eső", "havaseső", "csapadék",
            "hőmérséklet", "homerseklet", "hány fok", "hany fok", "fok lesz", "minimum", "maximum", "meleg lesz", "hideg lesz",
            "szél", "szeles", "szélsebesség", "vihar", "viharos", "front", "hidegfront", "melegfront",
            "ma", "holnap", "holnapután", "ma este", "reggel", "délután", "este",
            "lesz-e eső", "lesz e eső", "mennyi hó volt", "mennyi ho volt",
        ]
        return any(k in m for k in search_kw) or any(k in m for k in weather_kw)

    def _is_image_search_query(self, msg: str) -> bool:
        """Képkeresés kérés-e (keress kepet, mutass képet stb.) – beszélt nyelvi formák is."""
        m = (msg or "").strip().lower()
        phrases = [
            "keress kepet", "keress képet", "mutass kepet", "mutass képet",
            "keress egy kepet", "mutass egy kepet", "keress nekem egy kepet", "keress nekem kepet",
            "képet keress", "kepet keress", "képkeresés", "kepkereses", "kepre", "képre", "image",
            "küldj képet", "képet kérek", "kép róla", "fotó", "fénykép", "rajz", "illusztráció",
            "nézne ki", "hogy néz ki", "hogyan néz ki", "image of", "photo of",
        ]
        return any(p in m for p in phrases)

    def _extract_image_search_query(self, text: str) -> str:
        """Képkeresési kifejezés kinyerése az üzenetből."""
        t = (text or "").strip()
        for phrase in [
            "keress nekem egy kepet ", "keress egy kepet ", "keress kepet ", "keress képet ", "keress egy képet ",
            "mutass kepet ", "mutass képet ", "mutass egy kepet ", "mutass egy képet ",
            "keress egy kepre ", "mutass kepre ", "keress kepre ",
        ]:
            t = re.sub(re.escape(phrase), " ", t, flags=re.IGNORECASE)
        t = re.sub(r"^(?:keress|mutass|keres)\s+(?:egy\s+)?(?:kep|kép)(?:et|re)?\s*", " ", t, flags=re.IGNORECASE)
        t = re.sub(r"\s+", " ", t).strip()
        return t or "image"

    def _extract_location_for_weather(self, text: str) -> str:
        """Helynév kinyerése: magyar ragok és kérdő szavak eltávolítása."""
        t = (text or "").strip()
        # Mélykeresés / keresés szavak eltávolítása (ne maradjon benne, különben „milyen”/„lesz” miatt Magyarországot adnánk)
        for phrase in ["mélykeresés ", "melykeresés ", "mély keresés ", "mely keresés ", "deep research "]:
            t = re.sub(re.escape(phrase), " ", t, flags=re.IGNORECASE)
        # Kérdő/filler szavak eltávolítása (idő/idö/ido variánsok)
        for phrase in [
            "milyen idő lesz ", "milyen ido lesz ", "milyen idö lesz ", "milyen idő van ", "milyen ido van ", "milyen idö van ",
            "időjárás ", "idojárás ", "idöjaras ", "keress az interneten ", "keress a weben ", "ma ", "holnap ", "weather ",
        ]:
            t = re.sub(re.escape(phrase), " ", t, flags=re.IGNORECASE)
        t = re.sub(r"\s+(?:esni fog|eső|esö)(?:\s+e)?\s*$", " ", t, flags=re.IGNORECASE)
        # Magyar ragok a helynév végén: -ban, -ben, -on, -en stb.
        t = re.sub(r"-(?:ban|ben|on|en|nál|nél|ra|re|ba|be)\s*$", "", t, flags=re.IGNORECASE)
        # Magyar -n ragozás: Nagykanizsán, Nagykanizsän -> Nagykanizsa
        t = re.sub(r"[áä](n)\s*$", r"a", t, flags=re.IGNORECASE)
        t = re.sub(r"é(n)\s*$", r"e", t, flags=re.IGNORECASE)
        t = re.sub(r"\s+", " ", t).strip()
        # Időszavak (ma, holnap) nem helyek – ne adjuk vissza őket
        if not t or any(w in t.lower() for w in ["milyen", "lesz", "esni", "fog", "ma", "holnap", "tegnap", "reggel", "délben", "este"]):
            return "Magyarország"
        if t.lower() in ("ma", "holnap", "tegnap", "reggel", "dél", "este", "nap", "hét"):
            return "Magyarország"
        return t

    def _get_user_residence(self, body: dict, __user__: Optional[dict]) -> str:
        """Tartózkodási hely: body / __user__ / env USER_WEATHER_LOCATION. Ha nincs megadva → Budapest."""
        for source in (body or {}, __user__ or {}):
            if not isinstance(source, dict):
                continue
            for key in ("user_location", "location", "city", "tartózkodási_hely", "weather_location"):
                v = source.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        loc = (os.environ.get("USER_WEATHER_LOCATION") or "").strip()
        return loc if loc else "Budapest"

    async def _fetch_location_from_ip(self, body: dict) -> Optional[str]:
        """IP alapú tartózkodási hely. Első: ipapi.co, fallback: ip-api.com (korlátozás elkerülésére).
        body['client_ip'] / 'user_ip' / 'x_forwarded_for' = kliens IP (opcionális). Vissza: pl. 'Budapest, Hungary'."""
        ip = None
        if isinstance(body, dict):
            ip = body.get("client_ip") or body.get("user_ip")
            if not ip and body.get("x_forwarded_for"):
                xff = body["x_forwarded_for"]
                ip = (xff.split(",")[0].strip() if isinstance(xff, str) else (xff[0] if xff else None))

        def _parse_ipapi(data: dict) -> Optional[str]:
            if not isinstance(data, dict):
                return None
            city = (data.get("city") or "").strip()
            country = (data.get("country_name") or "").strip()
            if city and country:
                return f"{city}, {country}"
            if city:
                return city
            region = (data.get("region") or "").strip()
            if region and country:
                return f"{region}, {country}"
            return region or country or None

        # 1) ipapi.co
        url1 = f"https://ipapi.co/{ip}/json/" if ip else "https://ipapi.co/json/"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url1, headers={"User-Agent": "MagyarRouter/1.0"}, timeout=aiohttp.ClientTimeout(total=4)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        loc = _parse_ipapi(data)
                        if loc:
                            return loc
        except Exception:
            pass

        # 2) Fallback: ip-api.com (másodlagos, ingyenes 45 req/min)
        url2 = "http://ip-api.com/json/" + (ip or "") + "?fields=status,city,regionName,country"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url2, headers={"User-Agent": "MagyarRouter/1.0"}, timeout=aiohttp.ClientTimeout(total=4)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and data.get("status") == "success":
                            city = (data.get("city") or "").strip()
                            country = (data.get("country") or "").strip()
                            if city and country:
                                return f"{city}, {country}"
                            region = (data.get("regionName") or "").strip()
                            if region and country:
                                return f"{region}, {country}"
                            return city or region or country or None
        except Exception:
            pass
        return None

    def _query_suggests_deep_research(self, msg: str) -> bool:
        """A kérdés tartalma mélykeresést sugall-e – beszélt nyelv, stabilabb trigger."""
        m = (msg or "").strip().lower()
        # Hírek (mai hír, hírek, news) NEM indítanak mélykeresést – egyszerű keresés + Jina elég
        phrases = [
            "keress meg", "keressd meg", "keressed meg", "keresd meg", "keres meg", "keress rá", "keress ra",
            "keress nekem", "keress nekem egy", "nevezetesség", "látnivaló", "meg lehet nézni",
            "alapították", "alapítottak", "alapította", "története", "tortenete", "történetét",
            "összefoglalás", "foglald össze", "foglalj össze", "részletesen", "tudományos", "kutatás", "research",
            "mi a legjobb", "legjobb", "mit javasol", "ajánl",
            "részletes elemzés", "elemezd", "hasonlítsd össze", "összehasonlítás", "forrásokkal", "hivatkozásokkal",
            "tanulmány", "statisztika", "adatok alapján", "történelmi háttér", "okok és következmények",
            "mélyebben", "bővebben", "teljes áttekintés", "magyarázd el részletesen",
        ]
        return any(p in m for p in phrases)

    def _is_deep_research_query(self, msg: str, use_melykereses_override: Optional[bool] = None) -> bool:
        """Mélykeresés kell-e (Reader + synthesis). Ha mélykeresés BE van kapcsolva → minden webes keresés mélykeresés."""
        use = use_melykereses_override if use_melykereses_override is not None else getattr(self.valves, "USE_MELYKERESES", False)
        if not use:
            return False
        # Mélykeresés BE (toggle/Filter): minden webes keresés mélykeresés, nem csak a listás kifejezések
        if use is True:
            return True
        m = (msg or "").strip().lower()
        # Hírek (mai hír, hírek, news) NEM mélykeresés – egyszerű keresés + Jina
        phrases = [
            "keress meg", "keressd meg", "keressed meg", "kérd meg", "keresd meg", "keres meg", "keress rá", "keress ra",
            "alapították", "alapítottak", "alapította", "története", "tortenete", "történetét",
            "összefoglalás", "osszefoglalás", "foglald össze", "foglalj össze",
            "részletesen", "reszletesen", "részletes elemzés", "elemezd", "összehasonlítás", "forrásokkal",
            "tudományos", "kutatás", "research", "mélykeresés", "mély keresés", "tanulmány", "statisztika",
            "mi a legjobb", "legjobb", "mit javasol",
        ]
        return any(p in m for p in phrases)

    def _build_search_query(self, msg: str, weather_location_override: Optional[str] = None) -> str:
        """Időjárás – világméretű keresés (aktuális vagy történeti). Mai híreknél a mai dátum a keresésbe.
        weather_location_override: ha nincs hely a mondatban, a hívó átadhatja a tartózkodási helyet."""
        m = (msg or "").strip().lower()
        weather_in_msg = any(w in m for w in [
            "időjárás", "idojárás", "idö", "ido", "eső", "esö", "esni fog",
            "weather", "előrejelzés", "milyen idő", "milyen ido", "lesz-e eső",
            "hó", "ho", "volt", "voltak", "snowfall", "snow",
        ])
        if weather_in_msg:
            is_historical = self._is_historical_weather_query(msg)
            if is_historical:
                # Történeti: a teljes üzenet kerül keresésre (dátum + hó/eső + hely)
                return f"weather historical {msg.strip()} snowfall snow cm"
            location = (weather_location_override or "").strip() or self._extract_location_for_weather(msg)
            if not location or (location or "").strip().lower() == "magyarország":
                location = "Budapest"
            return f"weather Wetter {location} forecast ma"
        # Mai hírek: a mai dátum a keresésbe, hogy frissebb találatok jöjjenek (ne 02.10 ha ma 02.15)
        if any(k in m for k in ["mai hír", "mai hírek", "a nap legfontosabb", "a nap hírei", "foglald össze a nap", "nap hírei"]):
            date_str, _, _ = _get_current_datetime_str()
            return f"{msg.strip()} {date_str}"
        return msg

    def _classify_search_intent(self, msg: str) -> str:
        """Szándékfelismerő: hír, időjárás, tudományos, vagy általános. Híreknél time_range=day hasznos."""
        m = (msg or "").strip().lower()
        if any(k in m for k in ["mai hír", "mai hírek", "a nap legfontosabb", "a nap hírei", "foglald össze a nap", "nap hírei", "legfontosabb hír"]):
            return "news"
        if any(w in m for w in ["időjárás", "idojárás", "milyen idő", "weather", "előrejelzés", "eső", "hó"]):
            return "weather"
        if any(w in m for w in ["tudományos", "kutatás", "research", "tanulmány", "statisztika", "elemzés"]):
            return "scientific"
        return "general"

    async def _search_web(self, query: str, max_results: int = 5, time_range: Optional[str] = None) -> List[dict]:
        """Webes keresés SearXNG-vel. time_range: pl. 'day' (híreknél frissebb találatok)."""
        results = []
        try:
            url = f"{self.searxng_url}/search"
            params = {"q": query, "format": "json"}
            if time_range:
                params["time_range"] = time_range
            headers = {"User-Agent": "MagyarRouter/1.0", "Accept": "application/json"}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return results
                    data = await resp.json()
            for r in data.get("results", [])[:max_results]:
                results.append({
                    "title": r.get("title", "")[:200],
                    "snippet": (r.get("content") or r.get("snippet", ""))[:300],
                    "url": r.get("url", ""),
                })
        except Exception:
            pass
        return results

    async def _search_images(self, query: str, max_results: int = 2) -> List[dict]:
        """Képkeresés SearXNG-vel (categories=images)."""
        results = []
        try:
            url = f"{self.searxng_url}/search"
            params = {"q": query, "format": "json", "categories": "images"}
            headers = {"User-Agent": "MagyarRouter/1.0", "Accept": "application/json"}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        return results
                    data = await resp.json()
            for r in data.get("results", [])[:max_results]:
                img_url = r.get("img_src") or r.get("url") or r.get("thumbnail_src")
                if img_url and img_url.startswith("http"):
                    results.append({"title": r.get("title", "")[:100], "url": img_url})
        except Exception:
            pass
        return results

    @staticmethod
    def _strip_html(html: str, max_len: int = 8000) -> str:
        """Egyszerű HTML → szöveg (Reader lapolvasáshoz)."""
        if not html:
            return ""
        text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
        text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()[:max_len]

    async def _fetch_page_text(self, session: aiohttp.ClientSession, url: str, timeout: float = 5.0) -> str:
        """Egy oldal letöltése és szöveg kinyerése (Reader). Wikipedia-barát User-Agent."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8",
        }
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    return ""
                body = await resp.text()
                return self._strip_html(body, max_len=4000)
        except Exception:
            return ""

    async def _fetch_jina_reader(self, url: str, max_chars: int = 4000, timeout_sec: float = 10) -> Tuple[str, int]:
        """Oldal tartalom letöltése Jina Readerrel (LLM-barát markdown). Vissza: (szöveg, http_status).
        403/429 esetén ne próbáljunk újra (retry)."""
        if not url or not str(url).strip().startswith("http"):
            return ("", 0)
        base = (os.environ.get("JINA_READER_URL", "http://10.0.0.239:3000") or "http://10.0.0.239:3000").strip().rstrip("/")
        target_enc = urllib.parse.quote(str(url).strip(), safe="/:")
        jina_url = base + "/" + target_enc
        try:
            headers = {
                "User-Agent": "MagyarRouter/1.0 (Jina Reader)",
                "Accept": "text/plain,text/markdown,*/*",
                "X-Respond-With": "markdown",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    jina_url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_sec)
                ) as resp:
                    status = resp.status
                    if status != 200:
                        return ("", status)
                    text = await resp.text()
                    if not text:
                        return ("", status)
                    stripped = (text or "").strip()
                    if stripped.startswith("{") and '"error"' in stripped[:200]:
                        return ("", status)
                    return (stripped[:max_chars], status)
        except Exception:
            return ("", 0)

    async def _run_deep_research(self, query: str, search_intent: Optional[str] = None):
        """Mélykeresés: SearXNG + lapolvasás + Ollama szintézis. Async generator: yield progress, végén {ok, content, raw_context?}.
        search_intent: 'news'|'weather'|'scientific'|'general' – híreknél time_range=day a frissebb találatokhoz."""
        out: dict = {"ok": False, "content": "", "raw_context": ""}

        yield "*🔍 SearXNG keresés…*\n"
        time_range = "day" if search_intent == "news" else None
        results = await self._search_web(query, max_results=10, time_range=time_range)
        if not results:
            out["content"] = "Nem található keresési eredmény."
            yield out
            return

        n = len(results)
        yield f"*✓ Találatok: {n} db*\n"
        for i, r in enumerate(results, 1):
            title = (r.get("title") or "")[:60]
            raw_url = r.get("url") or ""
            url_disp = raw_url[:70] + "…" if len(raw_url) > 70 else raw_url
            yield f"  • {i}. **{title}**  \n    `{url_disp}`\n"

        parts = [f"Kérdés: {query}\n"]
        for i, r in enumerate(results, 1):
            parts.append(f"\n--- Forrás {i}: {r.get('title', '')} ---\n{r.get('snippet', '')}\nURL: {r.get('url', '')}")

        # Lapok lekérése: max MAX_DEEP_RESEARCH_PAGES (default 10), párhuzamosan (semaphore), 403/429 nem retry
        max_pages = int(os.environ.get("MAX_DEEP_RESEARCH_PAGES", "10").strip() or "10")
        urls_to_fetch = [r["url"] for r in results if r.get("url") and str(r["url"]).startswith("http")][:max_pages]
        total_urls = len(urls_to_fetch)
        jina_semaphore = asyncio.Semaphore(3)

        async def _fetch_one(idx: int, url: str) -> Tuple[int, str]:
            async with jina_semaphore:
                text, status = await self._fetch_jina_reader(url, max_chars=3500)
                if (not text or len(text) <= 100) and status not in (403, 429):
                    text2, _ = await self._fetch_jina_reader(url, max_chars=3500, timeout_sec=30)
                    if text2 and len(text2) > 100:
                        text = text2
                return (idx, text or "")

        jina_results: dict = {}
        if urls_to_fetch:
            yield "*📖 Lapok olvasása (Jina Reader)…*\n"
            tasks = [_fetch_one(idx, url) for idx, url in enumerate(urls_to_fetch, 1)]
            done = 0
            for coro in asyncio.as_completed(tasks):
                idx, text = await coro
                done += 1
                jina_results[idx] = text or ""
                title = (results[idx - 1].get("title") or "N/A")[:50]
                url_short = (urls_to_fetch[idx - 1] or "")[:65] + ("…" if len(urls_to_fetch[idx - 1] or "") > 65 else "")
                yield f"  → {done}/{total_urls}. lap: **{title}**  \n    `{url_short}`\n"
                if text and len(text) > 100:
                    yield f"  ✓ Szöveg kinyerve ({len(text)} karakter)\n"
                else:
                    yield f"  ⚠ Nem sikerült vagy túl rövid\n"
            for idx in sorted(jina_results.keys()):
                text = jina_results.get(idx) or ""
                if text and len(text) > 100:
                    parts.append(f"\n--- Oldal szöveg (Forrás {idx}) ---\n{text[:3500]}")

        # Markdown tisztítás: navigáció/lábléc minták csökkentése a kontextusablak és a szintézis gyorsításához
        def _clean_markdown_for_synthesis(raw: str) -> str:
            if not raw or len(raw) < 500:
                return raw
            s = re.sub(r"\[[\s\d\.\-]+\]\s*\(\s*#[\w\-]+\s*\)", " ", raw)
            s = re.sub(r"(Cookie|Sütik|Privacy|Adatvédelem|Impressum|Kontakt|Contact|Menu|Navigation|Footer|Lábléc)[\s:]*[\s\S]{0,200}?(?=\n\n|\Z)", " ", s, flags=re.IGNORECASE)
            s = re.sub(r"\n{3,}", "\n\n", s)
            return s.strip()

        context = "\n".join(parts)
        context = _clean_markdown_for_synthesis(context)
        if len(context) > 28000:
            context = context[:28000] + "\n[... vágva ...]"

        # Nyers kontextus mindig megmarad – ha a szintézis üres, a végső modell ebből foglalhat össze
        raw_max = int(os.environ.get("SEARCH_CONTEXT_MAX", "14000"))
        out["raw_context"] = (context[:raw_max] + "\n[... vágva ...]" if len(context) > raw_max else context)

        # gemma2 gyakran ignorálja a megadott forrást és „nincs internetem” választ ad; qwen3 jobban követi az utasítást
        model = os.environ.get("DEEP_RESEARCH_MODEL", "qwen3:latest")
        timeout_sec = int(os.environ.get("DEEP_RESEARCH_TIMEOUT", "120"))
        yield f"*🤖 Ollama szintézis ({model})…*\n"

        date_str, weekday, _ = _get_current_datetime_str()
        system_prompt = (
            "Te egy összefoglaló asszisztens vagy. A felhasználó által MÁR LEKÉRDEZETT források szövege kerül alább: "
            "ezeket az oldalakat már letöltötték neked, NEM kell internethez férned. A feladatod: ebből a megadott szövegből "
            "válaszolj RÉSZLETESEN, magyarul. Csak a forrásokban szereplő információt használd, ne találj ki semmit. "
            "TILOS azt írni, hogy nincs internet-hozzáféréded vagy hogy keress rá Google-n – a tartalom ALANT megvan. "
            "Ha dátumok, számok, nevek vannak a forrásokban, használd őket. Formáld át olvasható, összefüggő szöveggé. "
            f"A MAI DÁTUM: {date_str} ({weekday}). Ha a felhasználó a mai híreket kéri, a mai ({date_str}) dátummal ellátott tartalmat helyezd előtérbe; a régebbi cikkeket jelöld vagy mellékesnek tekintsd."
        )
        # Időjárás kérés: „milyen idő lesz” = időjárás, NEM kora déli idő – egyértelműsítés a modellnek
        q_lower = (query or "").lower()
        if "weather" in q_lower or "wetter" in q_lower or "forecast" in q_lower:
            system_prompt += (
                "\n\n🚨 IDŐJÁRÁS KÉRDÉS: A felhasználó IDŐJÁRÁS-ELŐREJELZÉST kért („milyen idő lesz” = milyen időjárás lesz ma/holnap), "
                "NEM a pontos órát (nem kell 15:30 vagy időzóna). A források időjárás oldalak. "
                "Foglald össze a hőmérsékletet (°C), csapadékot, szél, időjárási viszonyokat ma és holnapra. NE írd meg a pontos órát."
            )
        user_message = (
            "Az alábbi blokk a lekérdezett híroldalak/források szövege. Foglald össze ezt a tartalmat a kérdésre, "
            "ne mondd, hogy nincs hozzáférésed – a szöveg itt van.\n\n--- FORRÁSOK ---\n\n" + context
        )

        try:
            chat_url = f"{self.ollama_base_url}/api/chat"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "stream": True,
            }
            model_lower = (model or "").lower()
            if any(tm in model_lower for tm in _THINKING_MODELS):
                payload["think"] = "medium" if "gpt-oss" in model_lower else True

            full_content_parts: list = []
            in_thinking = False
            first_chunk = True

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    chat_url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout_sec)
                ) as resp:
                    if resp.status != 200:
                        out["content"] = f"A szintézis sikertelen (HTTP {resp.status})."
                        yield out
                        return

                    buffer = ""
                    stream_done = False
                    async for chunk_bytes in resp.content.iter_chunked(256):
                        if stream_done:
                            break
                        if not chunk_bytes:
                            continue
                        buffer += chunk_bytes.decode("utf-8", errors="ignore")
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line or (not line.startswith("data:") and not line.startswith("{")):
                                continue
                            data_str = (line[5:].strip() if line.startswith("data:") else line).strip()
                            if not data_str or data_str == "[DONE]" or data_str == "{}":
                                continue
                            try:
                                obj = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(obj, dict):
                                continue

                            thinking_content = ""
                            if "thinking" in obj:
                                thinking_content = obj.get("thinking", "")
                            elif "message" in obj and isinstance(obj["message"], dict):
                                thinking_content = obj["message"].get("thinking", "")

                            if thinking_content:
                                if not in_thinking:
                                    in_thinking = True
                                    if not first_chunk:
                                        yield "\n\n"
                                    yield "**💭 Gondolkodás (szintézis):**\n\n"
                                yield thinking_content

                            content = ""
                            if "message" in obj and isinstance(obj["message"], dict):
                                content = obj["message"].get("content", "")
                            if not content and "response" in obj:
                                content = obj.get("response", "")

                            if content:
                                full_content_parts.append(content)
                                if in_thinking:
                                    yield "\n\n---\n\n**Összefoglalás:** "
                                    in_thinking = False
                                yield content

                            if first_chunk and (thinking_content or content):
                                first_chunk = False

                            if obj.get("done"):
                                if in_thinking:
                                    yield "\n\n---"
                                stream_done = True
                                break

            out["ok"] = True
            out["content"] = ("".join(full_content_parts) or "").strip()
        except asyncio.TimeoutError:
            out["content"] = "A mélykeresés időtúllépés miatt megszakadt. Próbáld rövidebb kérdéssel."
        except Exception as e:
            out["content"] = f"Mélykeresés hiba: {e!s}"
        yield out

    def _location_to_slug(self, location: str) -> str:
        """Helynév → URL slug (oberndorf-bei-salzburg, nagykanizsa)."""
        s = (location or "").strip().lower()
        s = s.replace("ä", "a").replace("ö", "o").replace("ü", "u")
        s = re.sub(r"[^\w\s\-]", "", s)
        s = re.sub(r"\s+", "-", s).strip("-")
        return s or "budapest"

    def _parse_weather_ma_holnap(self, html: str) -> Optional[str]:
        """Ma + Holnap előrejelzés kinyerése Meteoblue/Bergfex HTML-ből."""
        date_str, weekday, _ = _get_current_datetime_str()
        tomorrow = datetime.now() + timedelta(days=1)
        tw = ("hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap")
        tomorrow_wd = tw[tomorrow.weekday()]
        tomorrow_str = tomorrow.strftime("%Y.%m.%d")

        ma_parts, holnap_parts = [], []
        # Meteoblue/Bergfex/Weather.com: Today 7 °C 0 °C vagy Today 56 °F 22 °F
        for m in re.finditer(r"(?:Today|Ma|Heute)\s+(-?\d+)\s*°\s*([FC])?\s+(-?\d+)\s*°\s*[FC]?(?:\s+(\d+)\s*(?:km/h|mph))?(?:\s+([\d\-]+\s*(?:mm|cm|in)))?", html, re.I):
            t1, t2 = int(m.group(1)), int(m.group(3))
            if (m.group(2) or "").upper() == "F":
                t1, t2 = int((t1 - 32) * 5 / 9), int((t2 - 32) * 5 / 9)
            tmax, tmin = max(t1, t2), min(t1, t2)
            wind = (m.group(4) or "").strip()
            precip = (m.group(5) or "").strip()
            ma_parts.append(f"Hőmérséklet: {tmin}–{tmax} °C (max ~{tmax} °C)")
            if precip:
                ma_parts.append(f"Csapadék: {precip}")
            if wind:
                ma_parts.append(f"Szél: {wind} km/h")
            break
        for m in re.finditer(r"(?:Tomorrow|Holnap|Morgen)\s+(-?\d+)\s*°\s*([FC])?\s+(-?\d+)\s*°\s*[FC]?(?:\s+(\d+)\s*(?:km/h|mph))?(?:\s+([\d\-]+\s*(?:mm|cm|in)))?", html, re.I):
            t1, t2 = int(m.group(1)), int(m.group(3))
            if (m.group(2) or "").upper() == "F":
                t1, t2 = int((t1 - 32) * 5 / 9), int((t2 - 32) * 5 / 9)
            tmax, tmin = max(t1, t2), min(t1, t2)
            precip = (m.group(5) or "").strip()
            holnap_parts.append(f"Hőmérséklet: {tmin}–{tmax} °C")
            if precip:
                holnap_parts.append(f"Csapadék: {precip}")
            break

        # Bergfex 9-Tage: 6°C 0°C 3cm 90% vagy 6°C 0°C 90%
        if not ma_parts or not holnap_parts:
            rows = re.findall(r"(-?\d+)°C\s+(-?\d+)°C(?:\s+[\d\-<]*cm)?\s*(\d+)%", html)
            if len(rows) >= 1:
                t1, t2, pct = rows[0][0], rows[0][1], rows[0][2]
                if not ma_parts:
                    ma_parts = [f"Hőmérséklet: {t2}–{t1} °C (max ~{t1} °C)", f"Csapadék: {pct}%"]
            if len(rows) >= 2:
                t1, t2, pct = rows[1][0], rows[1][1], rows[1][2]
                if not holnap_parts:
                    holnap_parts = [f"Hőmérséklet: {t2}–{t1} °C", f"Csapadék: {pct}%"]

        # Weather report szöveg – "chance of precipitation extremely high, exceeding 95%"
        if not ma_parts:
            for m in re.finditer(r"(?:precipitation|csapadék).*?(\d+)\s*%", html, re.I):
                if 50 <= int(m.group(1)) <= 100:
                    ma_parts.append(f"Csapadék valószínűség: {m.group(1)}%")
                    break

        lines = [f"MA, {date_str} ({weekday}):"]
        if ma_parts:
            lines.extend(f"  - {p}" for p in ma_parts)
        else:
            lines.append("  - (ellenőrizd a forrást)")
        lines.append(f"HOLNAP, {tomorrow_str} ({tomorrow_wd}):")
        if holnap_parts:
            lines.extend(f"  - {p}" for p in holnap_parts)
        else:
            lines.append("  - (ellenőrizd a forrást)")
        return "\n".join(lines)

    async def _scrape_weather_page(self, url: str, location: str) -> Optional[dict]:
        """Időjárás oldal közvetlen lekérése – Bergfex/Meteoblue HTML feldolgozás."""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status != 200:
                        return None
                    html = await resp.text()
            if len(html) < 500 or not any(k in html.lower() for k in ["°", "temp", "wetter", "weather", "hőmérséklet", "precipitation"]):
                return None

            # Ma + Holnap struktúra (prioritás)
            ma_holnap = self._parse_weather_ma_holnap(html)

            # Fallback: egyszerű hőmérséklet
            temp_max, temp_min = None, None
            all_temps = [
                int(m.group(1)) for m in re.finditer(r"(-?\d+)\s*°", html)
                if -40 <= int(m.group(1)) <= 50
            ]
            if len(all_temps) >= 2:
                temp_max, temp_min = max(all_temps[:8]), min(all_temps[:8])
            elif all_temps:
                temp_max = all_temps[0]
            temps = f"Max {temp_max}°C, Min {temp_min}°C" if (temp_max is not None and temp_min is not None) else (f"{temp_max}°C" if temp_max is not None else None)

            precip = None
            for m in re.finditer(r"(?:Niederschlag|precipitation|csapadék)[\s:]*(\d+)\s*%", html, re.I):
                v = int(m.group(1))
                if 0 <= v <= 100:
                    precip = f"{v}%"
                    break
            if not precip:
                for m in re.finditer(r"(\d{2,3})\s*%\s*(?:\d|l|1l|2h|W-|NW|SW)", html):
                    v = int(m.group(1))
                    if 40 <= v <= 100:
                        precip = f"{v}%"
                        break

            precip_cm = None
            for m in re.finditer(r"(\d+)\s*cm\s*(?:Niederschlag|Schnee|hó|snow)?", html, re.I):
                precip_cm = f"{m.group(1)} cm"
                break

            if temps or ma_holnap:
                date_str, weekday, _ = _get_current_datetime_str()
                if ma_holnap:
                    snippet = f"Időjárás – {location}\n\n{ma_holnap}"
                else:
                    parts = [f"Dátum: {date_str} ({weekday})", f"Hőmérséklet: {temps}"]
                    if precip:
                        parts.append(f"Csapadék valószínűség: {precip}")
                    if precip_cm:
                        parts.append(f"Csapadék mennyiség: {precip_cm}")
                    snippet = "\n".join(parts)
                return {
                    "title": f"Időjárás - {location}",
                    "snippet": snippet,
                    "url": url,
                }
        except Exception:
            pass
        return None

    def _guess_country_for_foreca(self, location: str) -> str:
        """Ország kitalálása Foreca URL-hez."""
        loc = (location or "").lower()
        if any(k in loc for k in ["austria", "salzburg", "wien", "oberndorf", "innsbruck", "graz"]):
            return "Austria"
        if any(k in loc for k in ["német", "germany", "münchen", "berlin"]):
            return "Germany"
        return "Hungary"

    async def _fetch_weather_direct(self, location: str) -> List[dict]:
        """Közvetlen időjárás lekérés több forrásból – első sikeres válasz számít."""
        loc = (location or "").strip()
        if not loc or loc.lower() == "magyarország":
            loc = "Budapest"
        slug = self._location_to_slug(loc)
        loc_enc = urllib.parse.quote((loc or "").replace(" ", "+"))
        country = self._guess_country_for_foreca(location)
        sites = [
            ("Meteoblue", f"https://www.meteoblue.com/en/weather/forecast/daily/{slug}"),
            ("Bergfex", f"https://www.bergfex.at/sommer/{slug}/wetter/"),
            ("Bergfex HU", f"https://hu.bergfex.com/{slug}/wetter/"),
            ("Foreca", f"https://www.foreca.hu/{country}/{slug}"),
            ("Weather.com", f"https://weather.com/weather/today/l/{loc_enc}"),
            ("Meteoblue AT", f"https://www.meteoblue.com/en/weather/forecast/daily/{slug}_austria_2769874"),
        ]
        if "oberndorf" not in slug and "salzburg" not in slug:
            sites = [s for s in sites if "austria_2769874" not in s[1]]
        for name, url in sites:
            r = await self._scrape_weather_page(url, loc)
            if r:
                return [r]
        return []

    def _is_weather_query(self, msg: str) -> bool:
        """Időjárás kérdés-e. Rövid szavak (pl. 'ho', 'ma') csak szóhatáron egyeznek, ne aktiválódjon 'hogyan' vagy 'magyarázd' miatt."""
        m = (msg or "").strip().lower()
        kw = ["időjárás", "idojárás", "idojaras", "milyen idő", "milyen ido", "eső", "esö", "weather", "előrejelzés", "elorejelzes"]
        for k in kw:
            if k in m:
                return True
        # "hó"/"ho" és "ma" csak önálló szóként (ne: hogyan → ho, magyarázd → ma)
        if re.search(r"\bho\b", m) or re.search(r"\bhó\b", m) or re.search(r"\bma\b", m):
            return True
        return False

    def _is_historical_weather_query(self, msg: str) -> bool:
        """Történeti / múltbeli időjárás kérdés-e (pl. 1925 december 21 mennyi hó volt)."""
        m = (msg or "").strip().lower()
        now = datetime.now()
        # Explicit múltbeli év: 19xx vagy 20xx ami nem az aktuális év
        past_years = re.findall(r"\b(19\d{2}|20[0-2]\d)\b", m)
        if past_years:
            for y in past_years:
                if int(y) < now.year:
                    return True
        # "volt" + időjárás: mennyi hó volt, esett-e, mekkora hó volt
        if any(w in m for w in ["volt", "voltak", "volt-e", "esett", "esett-e"]) and self._is_weather_query(msg):
            return True
        return False

    def _is_jupyter_health_query(self, msg: str) -> bool:
        """Jupyter elérhetőség / hibakeresés kérdés-e – bővített kulcsszavak."""
        m = (msg or "").strip().lower()
        kw = [
            "jupyter fut", "jupyter működik", "jupyter mukodik", "jupyter működik-e", "jupyter mukodik-e",
            "jupyter él", "jupyter el", "jupyter él-e", "jupyter el-e",
            "jupyter elérhető", "jupyter elerheto", "jupyter elérhető-e", "jupyter elerheto-e",
            "jupyter szerver", "jupyter status", "jupyter állapot", "jupyter allapot",
            "jupyter hibakeresés", "jupyter hibakereses", "jupyter hibakeresés", "jupyter debug",
            "is jupyter reachable", "jupyter reachable", "jupyter server", "jupyter erreichen",
            "elérhető-e a jupyter", "fut-e a jupyter",
        ]
        return any(k in m for k in kw)

    def _conversation_context_block(self, messages: List[dict], max_messages: int = 10, max_len_per_msg: int = 200) -> str:
        """Korábbi üzenetek rövid összefoglalása a kontextus megőrzéséhez."""
        if not messages or len(messages) <= 1:
            return ""
        lines = []
        # Utolsó N üzenet (user + assistant váltakozva)
        for m in messages[-(max_messages * 2) :]:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(p.get("text", p)) for p in content if isinstance(p, dict)
                )
            content = (content or "").strip()
            if not content or len(content) < 2:
                continue
            if len(content) > max_len_per_msg:
                content = content[: max_len_per_msg - 3] + "..."
            label = "Felhasználó" if role == "user" else "Asszisztens"
            lines.append(f"- {label}: {content}")
        if not lines:
            return ""
        return "💬 KORÁBBI BESZÉLGETÉS (összefoglalva):\n" + "\n".join(lines) + "\n\n"

    def _build_system_prompt(
        self,
        last_user: str,
        search_results_text: str,
        target: str,
        messages: Optional[List[dict]] = None,
        jupyter_status_text: Optional[str] = None,
    ) -> str:
        """Magyar nyelvi system prompt felépítése."""
        date_str, weekday, time_str = _get_current_datetime_str()
        msg = (last_user or "").strip().lower()
        now = datetime.now()

        is_time = any(p in msg for p in ["pontos idő", "mennyi az idő", "menyi az idő", "hány óra", "mikor"])
        is_date = any(p in msg for p in ["hányadika", "milyen dátum", "ma milyen nap", "ma melyik nap"])
        is_reasoning = any(p in msg for p in ["miért", "hogyan", "magyarázd", "működik", "logika"])
        is_weather = self._is_weather_query(last_user)

        parts = []
        # Mélykeresés hírek: azonnal az elején, hogy a modell ne „nem férhetek hozzá”-val kezdjen
        if search_results_text and ("MÉLYKERESÉS" in search_results_text or "NYERS FORRÁSOK" in search_results_text):
            parts.append(
                "🚨 FELADAT – A felhasználó a mai híreket kérte összefoglalásra. "
                "A válaszod ELSŐ mondata a hírek összefoglalása legyen (pl. „A nap legfontosabb hírei: …”). "
                "TILOS kezdeni „Sajnálom”-mal, „nem férhetek hozzá”-val vagy „nem tudok valós idejű”-val – a források az alábbi blokkban megvannak, használd őket. "
                "TILOS kérdezni a felhasználót (pl. „miben érdekelnek“, „hogyan szeretnéd kapni“) – csak összefoglald a fenti híreket.\n\n"
            )
        # Beszélgetés kontextusa – visszamenőleg tudja miről van szó
        if messages and len(messages) > 1:
            ctx_block = self._conversation_context_block(messages)
            if ctx_block:
                parts.append(ctx_block)
                parts.append(
                    "⚠️ FONTOS: A fenti korábbi üzenetek a jelenlegi beszélgetés részei. "
                    "Mindig ezen kontextus alapján válaszolj; tudd miről volt és miről van szó, és ne ismételd feleslegesen a már elmondottakat.\n\n"
                )
        # Aktuális dátum/idő MINDIG a system promptba
        parts.append(f"📅 AKTUÁLIS DÁTUM ÉS IDŐ: {date_str} ({weekday}), {time_str}. Ma = {date_str}.\n\n")
        parts.append(
            "MARKDOWN: **félkövér**, *dőlt*, - lista. Link: [szöveg](url). Kép: ![leírás](képcím_url). "
            "SOHA ne írj ![]([url](url)) – link legyen [szöveg](url), kép legyen ![alt](url) külön. "
            "Táblázat: | fejléc | fejléc |\n| --- | --- |\n| cella | cella |\n\n"
        )

        if is_time:
            parts.append(f"A felhasználó pontos időt kér: {date_str} ({weekday}), {time_str}.\n")
        elif is_date:
            parts.append(f"A felhasználó dátumot kér: {date_str} ({weekday}).\n")

        # Jupyter hibakeresés – elérhetőség státusz
        if jupyter_status_text:
            parts.append(f"\n🔧 JUPYTER HIBAKERESÉS – aktuális státusz:\n{jupyter_status_text}\n\n")

        # Kód futtatás: max biztonsági limit + felhasználói info
        parts.append(
            "KÓD FUTTATÁS BIZTONSÁG:\n"
            f"- A kód maximum {CODE_TIMEOUT_SEC} mp-ig futhat, ezután leáll (Execution timed out.).\n"
            "- Ha futtatható kódot adsz: MINDIG add hozzá a kód ELŐTT vagy UTÁN ezt az infót:\n"
            f"  „⏱️ MAXIMUM futtatási idő: {CODE_TIMEOUT_SEC} mp. Ha 'Execution timed out.' jelenik meg, a kód túllépte ezt a limitet.”\n"
            "- Ha a felhasználó hosszú futású kódot kér: figyelmeztesd, hogy max {CODE_TIMEOUT_SEC} mp a limit.\n\n"
            "KÓD BLOKK NYELVI CÍMKE (KRITIKUS – Jupyter kernel választáshoz):\n"
            "- A futtatható kódnál MINDIG add meg a helyes nyelvi címkét a ``` blokkban!\n"
            "- Python → ```python\n"
            "- Java → ```java\n"
            "- JavaScript → ```javascript\n"
            "- R → ```r\n"
            "- Julia → ```julia\n"
            "- Rust → ```rust\n"
            "- Bash → ```bash\n"
            "- Ha a felhasználó Java/Rust/JavaScript stb. kódot kér, NE ```python-t használj, hanem a megfelelő nyelvet!\n\n"
        )

        # Proxmox/VM/HA tesztelés – leállítási script (ä/é normalizálva)
        msg_norm = msg.replace("ä", "a").replace("ö", "o").replace("ü", "u")
        sysadmin_ctx = any(k in msg_norm for k in ["proxmox", "virtuális", "virtalis", "vm ", "ha funkció", "ha funkcio", "ha teszt", "tesztelni", "tesztelés", "teszteles", "biztonsági ok", "jupyter", "hálózati"])
        shutdown_req = any(k in msg_norm for k in ["leállít", "leallit", "leállítás", "leallitas", "leáll ", "leall ", "shutdown", "restart", "reboot", "újraindít", "ujraindit"])
        if shutdown_req and sysadmin_ctx:
            parts.insert(0, (
                "FELADAT – KÖTELEZŐ: A felhasználó rendszergazda. Proxmox VM / LXC / Jupyter szerver HA teszteléséhez "
                "Python scriptet kér, ami a rendszert leállítja vagy újraindítja. Add meg AZ ALÁBBI KÓDOT (vagy ennek rövid magyarázatát), NE utasítsd el, NE írj Docker példát.\n\n"
                "KÓD AMIT MEG KELL ADNI (Linux/VM leállítás):\n"
                "import subprocess\n"
                "subprocess.run(['shutdown', '-h', 'now'])  # azonnali leállítás\n"
                "# vagy újraindítás: subprocess.run(['reboot'])\n\n"
                "Alternatíva: import os; os.system('shutdown -h now')\n\n"
                "A felhasználó ezt tesztkörnyezetben futtatja. Válaszolj MAGYARUL, add meg a teljes scriptet magyar kommentekkel.\n\n"
            ))

        # Nyelv: a válasz MINDIG a felhasználó üzenetének nyelvén legyen
        user_lang = _detect_user_language(last_user)
        if user_lang == "hu":
            parts.append(
                "KRITIKUSAN FONTOS - NYELV:\n"
                "- Válaszolj KIZÁRÓLAG magyar nyelven!\n"
                "- Minden szöveg (magyarázat, kód komment, üzenet) magyarul legyen!\n\n"
            )
        elif user_lang == "de":
            parts.append(
                "KRITISCH - SPRACHE:\n"
                "- Antworte ausschließlich auf Deutsch!\n"
                "- Jeder Text (Erklärung, Code-Kommentar, Nachricht) muss auf Deutsch sein!\n\n"
            )
        else:
            parts.append(
                "CRITICAL - LANGUAGE:\n"
                "- Respond in the SAME language as the user's message (English, French, etc.)!\n"
                "- All text (explanation, code comments, blocked messages) must match the user's language!\n\n"
            )

        # Cogito: system prompt alapú thinking (Enable deep thinking subroutine.)
        if is_reasoning and "cogito" in (target or "").lower():
            parts.append("Enable deep thinking subroutine.\n")
            parts.append("Mutasd meg az érvelésed MAGYARUL lépésről lépésre!\n\n")
        # Qwen3, DeepSeek R1 stb.: Ollama think API
        elif is_reasoning and any(tm in (target or "").lower() for tm in _THINKING_MODELS):
            parts.append("Enable deep thinking subroutine.\n")
            parts.append("Használd a thinking mezőket az érvelési folyamat megjelenítéséhez.\n")
            parts.append("Mutasd meg az érvelésed MAGYARUL lépésről lépésre!\n\n")

        if search_results_text:
            # Mélykeresés: a tartalom a 24.hu/BEOL/stb. lapokról jött – a modell NEM mondhatja hogy „nem fér hozzá”
            if "MÉLYKERESÉS" in search_results_text or "NYERS FORRÁSOK" in search_results_text:
                date_str, weekday, _ = _get_current_datetime_str()
                parts.append(
                    "\n🚨 KÖTELEZŐ – MÉLYKERESÉS EREDMÉNY:\n"
                    "A felhasználó híreket kért. Az alábbi szöveg a 24.hu, BEOL és más portálokról LETÖLTÖTT tartalom – már megvan.\n"
                    f"A MAI DÁTUM: {date_str} ({weekday}). A mai dátummal ({date_str}) ellátott híreket helyezd előtérbe; a régebbi (pl. 02.10) cikkeket jelöld, hogy régebbi, vagy mellékesnek tekintsd.\n"
                    "A válaszod KIZÁRÓLAG ebből a szövegből kell hogy álljon: foglald össze a legfontosabb híreket, címekkel és rövid tartalommal.\n"
                    "TILOS írni: „nem férhetek hozzá“, „nem férhetek valós idejű információkhoz“, „nézd meg a hírportált“ – a források itt vannak fent, használd őket.\n\n"
                )
            parts.append(search_results_text)
            parts.append("\nHasználd fel a fenti keresési eredményeket a válaszodhoz.\n")
            # Hír/összefoglaló: a fenti tartalomból foglalj össze; ne mondd hogy „nem férsz hozzá”; ne időjárás linkeket javasolj
            is_news_or_summary = any(k in msg for k in ["hír", "hirek", "összefoglal", "osszefoglal", "foglald össze", "foglalj össze", "nap legfontosabb"])
            if is_news_or_summary and not is_weather:
                parts.append(
                    "\n⚠️ FONTOS: A felhasználó híreket/összefoglalást kért. A fenti tartalmat HASZNÁLD – "
                    "foglald össze belőle a legfontosabb híreket. NE írd hogy „nem férhetek hozzá“ vagy „nem férhetek valós idejű információkhoz“. "
                    "NE javasolj idokep.hu, Meteoblue, Bergfex linket; ha kevés adat van, javasold a hírportálokat (24.hu, Telex, Index, Hirstart).\n"
                )

            if is_weather:
                is_historical = self._is_historical_weather_query(last_user)
                if is_historical:
                    parts.append(
                        f"\n📜 TÖRTÉNETI IDŐJÁRÁS KÉRDÉS:\n"
                        f"- A felhasználó MÚLTBELI dátumra kér adatot (pl. 1925 december 21, mennyi hó volt).\n"
                        f"- A keresési eredményekben lévő RÉGI dátumokat (1925, 1985, 2021 stb.) HASZNÁLD – pont ezekre a dátummal kér információt!\n"
                        f"- Add meg a talált adatokat (hó magasság cm-ben, eső, hőmérséklet stb.) a kért dátummal.\n"
                        f"- Ha nem találsz pontos adatot, mondd el őszintén, és javasolj forrást (pl. időjárási archívum, kutatóintézet).\n"
                    )
                else:
                    tomorrow_dt = _get_now() + timedelta(days=1)
                    tw = ("hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap")
                    tomorrow_str = tomorrow_dt.strftime("%Y.%m.%d")
                    tomorrow_wd = tw[tomorrow_dt.weekday()]
                    parts.append(
                        f"\n⚠️⚠️⚠️ IDŐJÁRÁS VÁLASZ – KÖTELEZŐ ⚠️⚠️⚠️\n"
                        f"A válasz PONTOSAN így nézzen ki (konkrét adatokkal, NE placeholder szöveggel):\n\n"
                        f"**[Helynév] időjárás ({date_str})**\n\n"
                        f"**Ma, {weekday} ({date_str})**\n"
                        f"- Hőmérséklet: pl. 0–7 °C\n"
                        f"- Időjárás: pl. felhős, eső/havaseső\n"
                        f"- Csapadék: pl. 5–10 mm, 90%\n"
                        f"- Szél: pl. 7–12 km/h, észak\n\n"
                        f"**Holnap, {tomorrow_wd} ({tomorrow_str})**\n"
                        f"- Hőmérséklet: pl. -2–4 °C\n"
                        f"- Időjárás: pl. reggel felhős, déltől naposabb\n"
                        f"- Csapadék: pl. 2–5 cm hó\n"
                        f"- Szél: pl. enyhe, NW\n\n"
                        f"**Hasznos linkek:** A fenti MEGBÍZHATÓ FORRÁSOK URL-jei (Meteoblue, Bergfex, Foreca, Időkép).\n\n"
                        f"Dátum: MAI = {date_str}, HOLNAPI = {tomorrow_str}. SOHA ne írj 2021–2024 évszámot.\n"
                        f"TILOS írni: „nem férek hozzá“, „nem férhetek az aktuális időjárási adatokhoz“. "
                        f"Ha a fenti forrásokban nincs konkrét szám, add meg a linkeket és írd: A pontos adatokhoz nyisd meg a fenti linket (Meteoblue, Bergfex, Időkép).\n"
                    )

        return "".join(parts)

    async def pipe(self, body: dict, __user__=None):
        messages = list(body.get("messages", []))
        if not messages:
            yield "Nincs üzenet."
            return

        # Filter marker kinyerése és eltávolítása (tartalék, ha body['_melykereses'] nem jut el)
        mely_from_messages = None
        filtered = []
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                raw = m.get("content")
                if isinstance(raw, list):
                    c = " ".join(str(p.get("text", p)) for p in raw if isinstance(p, dict)).strip()
                else:
                    c = (raw or "").strip() if isinstance(raw, str) else str(raw or "").strip()
                if c == "[__MELYKERESES__=1]" or "[__MELYKERESES__=1]" in c:
                    mely_from_messages = True
                    continue
                if c == "[__MELYKERESES__=0]" or "[__MELYKERESES__=0]" in c:
                    mely_from_messages = False
                    continue
            filtered.append(m)
        if filtered != messages:
            messages = filtered
            body["messages"] = messages

        last_user = ""
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, list):
                    last_user = " ".join(
                        str(p.get("text", p)) for p in c if isinstance(p, dict)
                    ).strip() or str(c)
                else:
                    last_user = c if isinstance(c, str) else str(c)
                break

        # Chat parancs: !mélykeresés be / !mélykeresés ki – elsőbbség a többi beállítás felett
        clean_user, cmd_mely = _parse_melykereses_command(last_user)
        use_melykereses = cmd_mely
        if use_melykereses is None and "_melykereses" in body:
            use_melykereses = body.get("_melykereses")
        if use_melykereses is None and mely_from_messages is not None:
            use_melykereses = mely_from_messages
        if use_melykereses is None:
            uv = (__user__ or {}).get("valves") or {}
            use_melykereses = getattr(uv, "USE_MELYKERESES", uv.get("USE_MELYKERESES") if isinstance(uv, dict) else None)
        if use_melykereses is None:
            use_melykereses = getattr(self.valves, "USE_MELYKERESES", False)
        if use_melykereses is None:
            use_melykereses = os.environ.get("USE_MELYKERESES", "0").lower() in ("1", "true", "yes")
        # Ha a filter BE van de nem jut el (body/marker), de a kérdés egyértelműen „keress nekem” / nevezetesség → mélykeresés
        if not use_melykereses and self._query_suggests_deep_research(last_user):
            use_melykereses = True
        # Ha a mondat bárhol tartalmazza a „mélykeresés” (vagy hasonló) szót → minden körülmények között mélykeresés
        msg_lower = (last_user or "").lower()
        _mely_triggers = (
            "mélykeresés", "melykeresés", "mély keresés", "mely keresés",
            "melykereses", "mélykereses", "deep research",
        )
        if any(t in msg_lower for t in _mely_triggers):
            use_melykereses = True

        # Ha parancs volt, a modell ne lássa – tisztított üzenet
        if clean_user != last_user:
            messages = list(messages)
            for i in range(len(messages) - 1, -1, -1):
                if isinstance(messages[i], dict) and messages[i].get("role") == "user":
                    messages[i] = {**messages[i], "content": clean_user}
                    break
            last_user = clean_user

        target = await ModelRouter.select_model(last_user)

        # Webes keresés: normál kiváltók VAGY mélykeresés BE → minden üzenetnél neten keres
        needs_web_search = self._needs_web_search(last_user) or use_melykereses
        search_results_text = ""
        if needs_web_search:
            # BE = mélykeresés (Reader), KI = sima keresés (SearXNG + Jina)
            use_deep = bool(use_melykereses)
            is_weather = self._is_weather_query(last_user)
            is_historical = self._is_historical_weather_query(last_user)
            is_image_search = self._is_image_search_query(last_user)
            # Először kiszámoljuk a helyet (időjárásnál), hogy BE → LOKÁCIÓ → SearXNG sorrendben jelenjen meg
            effective_weather_location = None
            weather_location_from_message = False
            if is_weather and not is_historical:
                loc_from_msg = self._extract_location_for_weather(last_user)
                if loc_from_msg and (loc_from_msg or "").strip().lower() != "magyarország":
                    effective_weather_location = loc_from_msg.strip()
                    weather_location_from_message = True
                else:
                    effective_weather_location = self._get_user_residence(body, __user__)
                    if (effective_weather_location or "").strip().lower() == "budapest" and os.environ.get("USE_IP_WEATHER_LOCATION", "1").strip().lower() in ("1", "true", "yes"):
                        ip_loc = await self._fetch_location_from_ip(body)
                        if ip_loc:
                            effective_weather_location = ip_loc

            _debug_jina = os.environ.get("DEBUG_JINA_READER", "0").strip().lower() in ("1", "true", "yes")
            if _debug_jina:
                yield f"*[debug] use_deep={use_deep}, is_weather={is_weather}, is_image={is_image_search}*\n\n"
            # 1. Mélykeresés BE/KI jelzés
            yield ("*🔍 Mélykeresés BE (Reader) – folyamatban…*\n\n" if use_deep else "*🔍 Mélykeresés KI – egyszerű keresés (SearXNG)…*\n\n")
            # Hely-egyértelműsítés: ha a helynek nincs országa (pl. „Bergheim”), WEATHER_DEFAULT_COUNTRY (pl. Austria)
            if is_weather and effective_weather_location and "," not in (effective_weather_location or ""):
                default_country = (os.environ.get("WEATHER_DEFAULT_COUNTRY") or "").strip()
                if default_country:
                    effective_weather_location = f"{effective_weather_location.strip()}, {default_country}"

            # 2. Időjárásnál a mondatba illesztjük a helyet: „melykeresés milyen idö lesz (Bergheim, Austria) ma”
            if is_weather and not is_historical and effective_weather_location:
                s = last_user.strip()
                loc_in_parens = f"(**{effective_weather_location}**)"
                if re.search(r"\bma\b", s, re.IGNORECASE):
                    s = re.sub(r"\b(ma)\b", f"{loc_in_parens} \\1", s, count=1, flags=re.IGNORECASE)
                elif re.search(r"\bholnap\b", s, re.IGNORECASE):
                    s = re.sub(r"\b(holnap)\b", f"{loc_in_parens} \\1", s, count=1, flags=re.IGNORECASE)
                else:
                    s = f"{s} {loc_in_parens}"
                yield f"*📍 {s}*\n\n"

            results = []
            deep_research_done = False

            # Képkeresés: keress kepet X, mutass képet X
            if is_image_search:
                img_query = self._extract_image_search_query(last_user)
                # Magyar kifejezés → angol kiegészítés (SearXNG jobb találat angolul)
                hu_en = {"kina csaszar": "Chinese emperor", "kínai császár": "Chinese emperor", "napoleon": "Napoleon", "első ferenc": "Pope Francis"}
                q_lower = img_query.lower()
                for hu, en in hu_en.items():
                    if hu in q_lower:
                        img_query = f"{img_query} {en}"
                        break
                img_results = await self._search_images(img_query, max_results=4)
                if img_results:
                    search_results_text = "\n🖼️ KÉPKERESÉS EREDMÉNYEI – mutasd meg markdown formában:\n\n"
                    for im in img_results:
                        search_results_text += f"![{im.get('title', 'kép')}]({im.get('url', '')})\n\n"
                    search_results_text += "\nHasználd a fenti képeket a válaszodban.\n"
                else:
                    # Fallback: szöveges keresés
                    results = await self._search_web(img_query, max_results=5)

            # Mélykeresés: ha a mondatban szerepel a mélykeresés (vagy BE van), minden körülmények között ez fut először (időjárás/kép nélkül)
            elif use_deep and not is_image_search:
                search_query = self._build_search_query(last_user, weather_location_override=effective_weather_location)
                dr = None
                try:
                    search_intent = self._classify_search_intent(last_user)
                    async for item in self._run_deep_research(search_query, search_intent=search_intent):
                        if isinstance(item, dict) and "ok" in item:
                            dr = item
                            break
                        yield item
                    deep_research_done = True
                    if dr and dr.get("ok") and (dr.get("content") or "").strip() and len((dr.get("content") or "").strip()) > 150:
                        search_results_text = "\n🔍 MÉLYKERESÉS EREDMÉNYEI (Reader + szintézis):\n\n" + (dr["content"] or "").strip()
                        yield "*✓ Szintézis kész. Válasz írása…*\n\n"
                    elif dr and (dr.get("raw_context") or "").strip():
                        search_results_text = (
                            "\n🔍 MÉLYKERESÉS NYERS FORRÁSOK (a fenti lapok szövege – ezek alapján foglald össze a híreket, NE mondd hogy nem férsz hozzá):\n\n"
                            + (dr.get("raw_context") or "").strip()
                        )
                        yield "*✓ Források megvannak. Összefoglalás írása…*\n\n"
                    else:
                        search_results_text = (
                            "\n🔍 Mélykeresés lefutott, de nem érkezett forrás. "
                            "Javasold a hírportálokat: 24.hu, Telex, Index, Hirstart.\n\n"
                        )
                except Exception as e:
                    import traceback
                    err = str(e).strip() or repr(e)
                    yield f"*⚠️ Mélykeresés hiba ({err[:80]}) → egyszerű keresés…*\n\n"
                    import logging
                    logging.getLogger(__name__).warning("Mélykeresés hiba: %s\n%s", e, traceback.format_exc())
                    deep_research_done = False

            # Aktuális időjárás: csak ha NEM mélykeresés (közvetlen Bergfex/Meteoblue scrape)
            elif is_weather and not is_historical:
                location = effective_weather_location or self._extract_location_for_weather(last_user)
                results = await self._fetch_weather_direct(location)
                # Ha a scrape üres: egy időjárás oldal Jina Readerrel (Meteoblue)
                if not results:
                    loc = (location or "").strip() or effective_weather_location or "Budapest"
                    slug = self._location_to_slug(loc)
                    jina_weather_url = f"https://www.meteoblue.com/en/weather/forecast/daily/{slug}"
                    jina_text, _ = await self._fetch_jina_reader(jina_weather_url, max_chars=3500, timeout_sec=25)
                    if jina_text and len(jina_text) > 200:
                        date_str, weekday, _ = _get_current_datetime_str()
                        results = [{
                            "title": f"Időjárás - {loc} (Meteoblue)",
                            "snippet": f"Dátum: {date_str} ({weekday}).\n\n{jina_text[:3200]}",
                            "url": jina_weather_url,
                        }]

            # Ha nincs direct eredmény (és nem mélykeresés): SearXNG fallback
            if not results and not is_image_search and not deep_research_done:
                search_query = self._build_search_query(last_user)
                results = await self._search_web(search_query, max_results=5)

            if results:
                date_str, weekday, _ = _get_current_datetime_str()
                search_results_text = ""
                if is_image_search:
                    search_results_text = "🖼️ Képkeresés – talált linkek (mutasd meg, ne mondd hogy nem tudsz képet keresni!):\n\n"
                if is_weather and not is_historical:
                    search_results_text = (
                        f"\n🚨 KRITIKUS – DÁTUM (SOHA NE MÓDOSÍTSD!):\n"
                        f"A MAI DÁTUM: {date_str} ({weekday}). A holnapi: {(_get_now() + timedelta(days=1)).strftime('%Y.%m.%d')}.\n"
                        f"TILOS írni: 2021, 2022, 2023, 2024. Ha a snippet régi dátumot tartalmaz, FIGYELEM NÉLKÜL használd: {date_str}!\n\n"
                    )
                search_results_text += "\n🔍 KERESÉSI EREDMÉNYEK:\n\n"
                for i, r in enumerate(results, 1):
                    search_results_text += f"{i}. **{r.get('title', '')}**\n"
                    if r.get("snippet"):
                        search_results_text += f"   {r['snippet']}\n"
                    if r.get("url"):
                        search_results_text += f"   URL: {r['url']}\n"
                    search_results_text += "\n"
                # Jina Reader: egyszerű keresésnél egyszer: SearXNG (linkek) + Jina (top 2 lap szövege). Mélykeresésnél már lapok lettek lekérve (_fetch_page_text) → itt ne hívjuk a Jinát.
                _debug_jina = os.environ.get("DEBUG_JINA_READER", "0").strip().lower() in ("1", "true", "yes")
                if not is_image_search and not is_weather and results and not deep_research_done:
                    use_jina = os.environ.get("USE_JINA_READER", "1").lower() in ("1", "true", "yes")
                    if _debug_jina:
                        yield "*[debug] Jina: hívás (top 2 URL) → ReaderApi kapja a kérést.*\n\n"
                    if use_jina:
                        _skip_jina_domains = ("accuweather.com", "weather-forecast", "idokep.hu", "meteoblue.com", "bergfex.", "foreca.")
                        urls_jina = []
                        for r in results[:3]:
                            u = r.get("url")
                            if not u or not str(u).startswith("http"):
                                continue
                            u_lower = str(u).lower()
                            if any(s in u_lower for s in _skip_jina_domains):
                                continue
                            urls_jina.append((r, u))
                            if len(urls_jina) >= 2:
                                break
                        if urls_jina:
                            yield "*📄 Jina Reader: lapok lekérése…*\n\n"
                            search_results_text += "\n📄 OLDAL TARTALOM (Jina Reader – könnyebb értelmezés):\n\n"
                            jina_delay = float(os.environ.get("JINA_READER_DELAY_SEC", "2").strip() or "2")
                            for idx, (r, u) in enumerate(urls_jina, 1):
                                if idx > 1:
                                    await asyncio.sleep(max(0.5, jina_delay))
                                title = (r.get("title") or "Forrás")[:50]
                                content, _ = await self._fetch_jina_reader(u, max_chars=2000)
                                if content:
                                    search_results_text += f"--- {idx}. {title} ---\n{content}\n\n"
                                else:
                                    search_results_text += f"--- {idx}. {title} --- (nem sikerült)\n\n"
                            yield "*✓ Jina Reader kész.*\n\n"
                elif _debug_jina and results:
                    reason = []
                    if deep_research_done:
                        reason.append("mélykeresés kész")
                    if is_weather:
                        reason.append("időjárás")
                    if is_image_search:
                        reason.append("képkeresés")
                    yield f"*[debug] Jina: kihagyva ({', '.join(reason) or 'nincs találat'}).*\n\n"
                if is_weather and not is_historical:
                    loc = effective_weather_location or self._extract_location_for_weather(last_user)
                    slug = self._location_to_slug(loc)
                    img_results = await self._search_images(f"Weather Wetter {loc} forecast")
                    country = self._guess_country_for_foreca(loc)
                    search_results_text += (
                        f"\n📌 MEGBÍZHATÓ FORRÁSOK:\n"
                        f"- [Meteoblue](https://www.meteoblue.com/en/weather/forecast/daily/{slug})\n"
                        f"- [Bergfex](https://www.bergfex.at/sommer/{slug}/wetter/)\n"
                        f"- [Foreca](https://www.foreca.hu/{country}/{slug})\n"
                        f"- [Időkép](https://www.idokep.hu/)\n"
                    )
                    if img_results:
                        search_results_text += "\n🖼️ IDŐJÁRÁS KÉPEK (mutasd meg markdown formában):\n"
                        for im in img_results:
                            search_results_text += f"- ![{im.get('title', 'időjárás')}]({im.get('url', '')})\n"
            elif not search_results_text:
                # Nincs eredmény – időjárásnál mindig add meg a Bergfex/Meteoblue linket (képkeresésnél ne írjunk felül)
                loc = (effective_weather_location or self._extract_location_for_weather(last_user)) if is_weather else ""
                slug = self._location_to_slug(loc) if loc else "budapest"
                bergfex_url = f"https://www.bergfex.at/sommer/{slug}/wetter/"
                meteoblue_url = f"https://www.meteoblue.com/en/weather/forecast/daily/{slug}"
                search_results_text = (
                    "\n\n⚠️ A webes keresés nem adott konkrét adatot. "
                    "SOHA NE találj ki hőmérsékletet vagy csapadékot! "
                    f"Mondd el: ellenőrizze idokep.hu, Meteoblue {meteoblue_url} vagy Bergfex {bergfex_url}\n"
                )

        # Jupyter hibakeresés – elérhetőség ellenőrzés
        jupyter_status_text = ""
        if self._is_jupyter_health_query(last_user) and JUPYTER_URL:
            ok, msg = await _check_jupyter_reachable(JUPYTER_URL)
            jupyter_status_text = msg

        # Kontextus felső határ – ne küldjünk 50k+ karaktert, különben a modell lassul/timeout, válasz soha nem jön
        SEARCH_CONTEXT_MAX = int(os.environ.get("SEARCH_CONTEXT_MAX", "14000"))
        if len(search_results_text) > SEARCH_CONTEXT_MAX:
            search_results_text = search_results_text[:SEARCH_CONTEXT_MAX] + "\n\n[... tartalom vágva – túl hosszú.]"

        # Mélykeresés esetén a végső összefoglalást is a szintézis modell (qwen3) készítse (beleértve a „Mélykeresés lefutott…” fallbackot is)
        if search_results_text and ("MÉLYKERESÉS" in search_results_text or "NYERS FORRÁSOK" in search_results_text or "mélykeresés" in search_results_text.lower() or "Mélykeresés lefutott" in search_results_text):
            target = os.environ.get("DEEP_RESEARCH_MODEL", "qwen3:latest")

        # Magyar system prompt (messages = beszélgetés kontextusa)
        system_content = self._build_system_prompt(
            last_user, search_results_text, target, messages, jupyter_status_text or None
        )
        final_messages = [{"role": "system", "content": system_content}] + messages

        url = f"{self.ollama_base_url}/api/chat"
        payload = {"model": target, "messages": final_messages, "stream": True}
        # Thinking/exploring: Ollama think API – cogito, qwen3, deepseek-r1, gpt-oss stb.
        target_lower = (target or "").lower()
        if any(tm in target_lower for tm in _THINKING_MODELS):
            payload["think"] = "medium" if "gpt-oss" in target_lower else True

        # Gondolkodik jelzés - AZONNAL megjelenik
        model_short = target.split("/")[-1].split(":")[0] if "/" in target or ":" in target else target[:25]
        yield f"*⏳ Gondolkodik ({model_short})...*\n\n"

        try:
            first_chunk = True
            in_thinking = False
            full_content_parts = []  # Válasz összegyűjtése biztonsági szűrőhöz

            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=OLLAMA_TIMEOUT_SEC)) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        yield f"⚠️ Ollama hiba ({resp.status}): {err[:200]}"
                        return

                    buffer = ""
                    # Kisebb chunk (256) = gyorsabb stream, ne várjon a 1024 byte összegyűlésére
                    async for chunk_bytes in resp.content.iter_chunked(256):
                        if not chunk_bytes:
                            continue
                        buffer += chunk_bytes.decode("utf-8", errors="ignore")

                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line or (not line.startswith("data:") and not line.startswith("{")):
                                continue
                            data = (line[5:].strip() if line.startswith("data:") else line).strip()
                            if not data or data == "[DONE]" or data == "{}":
                                continue
                            try:
                                obj = json.loads(data)
                            except json.JSONDecodeError:
                                continue

                            if not isinstance(obj, dict):
                                continue

                            chunk_content = ""
                            thinking_content = ""

                            # REASONING / thinking mező
                            if "thinking" in obj:
                                thinking_content = obj.get("thinking", "")
                            elif "message" in obj and isinstance(obj["message"], dict):
                                thinking_content = obj["message"].get("thinking", "")

                            if thinking_content:
                                if not in_thinking:
                                    in_thinking = True
                                    if not first_chunk:
                                        yield "\n\n"
                                    yield "**💭 Gondolkodás:**\n\n"
                                yield thinking_content

                            # Normál content – összegyűjtjük, csak a végén szűrjük és adjuk ki
                            content = ""
                            if "message" in obj and isinstance(obj["message"], dict):
                                content = obj["message"].get("content", "")
                            if not content and "response" in obj:
                                content = obj.get("response", "")

                            if content:
                                full_content_parts.append(content)
                                if in_thinking:
                                    yield "\n\n---\n\n**Válasz:** "
                                    in_thinking = False
                                # Streameljük a választ is – azonnal megjelenik (ne várjon perceket)
                                yield content

                            if first_chunk and (thinking_content or content):
                                first_chunk = False

                            if obj.get("done"):
                                if in_thinking:
                                    yield "\n\n---"
                                # Válasz végén jelzés (kivéve gemma2)
                                if target and "gemma2" not in target.lower():
                                    yield "\n\n*✓ Válasz kész.*"
                                return

        except asyncio.TimeoutError:
            yield f"⚠️ Időtúllépés ({OLLAMA_TIMEOUT_SEC}s). A modell túl sokáig gondolkodott. Próbáld rövidebb kérdéssel vagy növeld OLLAMA_TIMEOUT_SEC-et."
        except Exception as e:
            err_msg = str(e).strip() or repr(e)
            yield f"⚠️ Hiba: {err_msg[:300]}"


async def _main_async():
    """Teszt futtatás aszinkron módban."""
    tests = [
        "szia",
        "mennyi az idő",
        "milyen dátum van ma",
        "ez a hét melyik nap",
        "milyen idő lesz ma",
        "lesz-e eső holnap",
        "időjárás Budapest",
        "írj egy egyszerű python kódot",
        "írj python kódot ami lekéri a pontos időt",
        "írj javascript kódot",
        "magyarázd el hogyan működik",
    ]
    for t in tests:
        result = await ModelRouter.select_model(t)
        print(f"{t!r} -> {result}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--jupyter", action="store_true", help="Jupyter elérhetőség ellenőrzés")
    p.add_argument("--jupyter-url", default=JUPYTER_URL or "http://localhost:8888", help="Jupyter URL")
    p.add_argument("--timeout", type=int, default=None, help="Kód timeout teszt (mp)")
    args = p.parse_args()

    if args.timeout is not None:
        print(f"Timeout teszt: {args.timeout} s")
        ok, msg, _ = _run_code_with_timeout("import time; time.sleep(999)", args.timeout)
        print(msg)
        sys.exit(0 if not ok else 1)

    if args.jupyter:
        print(f"Jupyter ellenőrzés: {args.jupyter_url}")
        ok, msg = asyncio.run(_check_jupyter_reachable(args.jupyter_url))
        print(msg)
        sys.exit(0 if ok else 1)

    asyncio.run(_main_async())
