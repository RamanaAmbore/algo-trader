import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import pandas as pd
import pyotp
import yaml
from babel.numbers import format_decimal

from backend.shared.helpers.date_time_utils import timestamp_indian


# Repo root = backend/shared/helpers/utils.py → parent × 4
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONFIG_DIR = _REPO_ROOT / "backend" / "config"


class CustomDict(dict):
    def __getitem__(self, key):
        # Check if any key in the dictionary ends with the specified key
        for k in self.keys():
            if k.endswith(key):
                return super().__getitem__(k)

        return None


# Load profile data from a YAML file
with open(_CONFIG_DIR / 'constants.yaml', 'r', errors='ignore', encoding='utf-8') as file:
    constants = yaml.safe_load(file)

# Load additional configuration data from a YAML file
with open(_CONFIG_DIR / 'frontend_config.yaml', 'r', encoding='utf-8', errors='ignore') as file:
    ramboq_config = yaml.safe_load(file)

# Load additional configuration data from a YAML file
with open(_CONFIG_DIR / 'secrets.yaml', 'r', encoding='utf-8', errors='ignore') as file:
    secrets = yaml.safe_load(file)  # Load YAML config file

# Load configuration from YAML file (merged deploy + connection settings)
with open(_CONFIG_DIR / 'backend_config.yaml', 'r', encoding='utf-8', errors='ignore') as file:
    config = yaml.safe_load(file)
    ramboq_deploy = config  # all deploy keys are now in backend_config.yaml

isd_codes = [f"{item['country']} ({item['code']})" for item in constants['isd_codes']]


def is_prod_branch() -> bool:
    """
    True on the main (prod) branch, False on any dev branch. This is the
    hard outer gate for mode 2 vs mode 3 — on non-main every broker-
    hitting action writes mode='paper' regardless of any DB flag; on
    main the `execution.paper_trading_mode` master toggle decides.
    """
    return config.get("deploy_branch") == "main"


def is_engine_idle() -> bool:
    """
    Should the running engine sit idle, skipping background tasks +
    broker calls? Returns True on non-main branches when
    `execution.dev_active` setting is False AND no sim/replay driver is
    running.

    Used by background tasks (_task_performance, _task_close,
    _task_sparkline_warm, _task_ticker_watchdog) and the KiteTicker
    auto-start gate to stop dev environments from hammering broker
    APIs when no operator is actively trading.

    Prod (main branch) always returns False so prod is unaffected —
    market data flows continuously as before.
    """
    if config.get("deploy_branch") == "main":
        return False

    # Sim / replay drivers running mean the operator is actively
    # working — keep engine awake regardless of dev_active. Use lazy
    # imports to avoid a circular import at module load time.
    try:
        from backend.api.algo.sim.driver import get_driver as _sim_drv
        if _sim_drv().active:
            return False
    except Exception:
        pass
    try:
        from backend.api.algo.replay.driver import get_replay_driver as _replay_drv
        if _replay_drv().active:
            return False
    except Exception:
        pass

    # Read the dev_active setting. Missing / unparseable → False (idle).
    try:
        from backend.shared.helpers import settings as _settings
        return not _settings.get_bool("execution.dev_active", False)
    except Exception:
        return True   # safest default — stay idle if we can't read


def is_enabled(cap: str) -> bool:
    """
    Is capability `cap` (e.g., 'genai', 'telegram', 'mail', 'notify_on_deploy',
    'market_feed', 'simulator') enabled in this environment?

    Precedence:
      1. DB setting at `notifications.<cap>_enabled` (or
         `notifications.<cap>`) — lets the operator toggle live from
         /admin/settings without a redeploy.
      2. `cap_in_prod.<cap>` (on main) or `cap_in_dev.<cap>` (on any
         other branch) from backend_config.yaml. main defaults to True
         when the key is missing; dev defaults to False (opt-in).

    To turn a capability off live, flip its DB toggle; to persist
    across container rebuilds set it in the cap_in_* YAML block.
    """
    # DB override takes precedence for the caps that ship with a
    # matching DB toggle (telegram / email / notify_on_deploy etc).
    try:
        from backend.shared.helpers import settings as _settings
        db_raw = _settings._lookup_raw(f"notifications.{cap}_enabled")
        if db_raw is None:
            db_raw = _settings._lookup_raw(f"notifications.{cap}")
        if db_raw is not None:
            return str(db_raw).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        # Settings module not ready at import time; fall through to YAML.
        pass

    branch = config.get('deploy_branch')
    section = 'cap_in_prod' if branch == 'main' else 'cap_in_dev'
    caps = config.get(section) or {}
    default = branch == 'main'  # prod defaults on, dev defaults off
    if isinstance(caps, dict):
        return bool(caps.get(cap, default))
    # Legacy scalar cap_in_* True/False — treat as blanket gate
    return bool(caps)


def capitalize(text):
    """
    Capitalize text if it doesn't already contain uppercase characters.
    """
    return text if isinstance(text, (int, float)) or any([x.isupper() for x in text]) else text.title()


def generate_totp(totp_key):
    """Generate a valid TOTP using the secret key."""
    return pyotp.TOTP(totp_key).now()


def to_decimal(value, precision="0.01"):
    """Convert float to Decimal with specified precision."""
    return Decimal(value).quantize(Decimal(precision), rounding=ROUND_DOWN)




def round_down_to_interval(dt: datetime, interval_minutes: int) -> datetime:
    total_minutes = dt.hour * 60 + dt.minute
    rounded_total_minutes = (total_minutes // interval_minutes) * interval_minutes

    rounded_hour = rounded_total_minutes // 60
    rounded_minute = rounded_total_minutes % 60

    return dt.replace(hour=rounded_hour % 24, minute=rounded_minute, second=0, microsecond=0)


def get_cycle_date(hours=8, mins=0):
    now = timestamp_indian()
    today_cutoff = now.replace(hour=hours, minute=mins, second=0, microsecond=0)

    if now >= today_cutoff:
        dt = now.date()
    else:
        dt = (now - timedelta(days=1)).date()
    return dt


def get_nearest_time(from_hour: int = 9, from_min: int = 0, to_hour: int = 23, to_min: int = 30,
                     interval: int = 10) -> str:
    now = timestamp_indian()
    from_time = now.replace(hour=from_hour, minute=from_min, second=0, microsecond=0)
    to_time = now.replace(hour=to_hour, minute=to_min, second=0, microsecond=0)

    # Handle time window crossing midnight

    in_window = from_time <= now <= to_time


    if in_window:
        rounded_time = round_down_to_interval(now, interval)
        return rounded_time.strftime("%d-%b-%y %H:%M")
    else:
        # Assume get_cycle_date returns a date object
        cycle_date = get_cycle_date(hours=9, mins=0)  # e.g., datetime.date(2025, 9, 7)

        # Combine with fixed time (23:30)
        fixed_datetime = datetime.combine(cycle_date, datetime.min.time()).replace(hour=23, minute=30)

        # Format as desired
        return fixed_datetime.strftime("%d-%b-%y %H:%M")


_MASK_RE = re.compile(r'\d')

# Account → masked-code registry, built from the live broker account
# list at Connections.rebuild_from_db time. Each account maps to:
#     <broker letter><1-based ordinal within broker>####
# Example registry with the operator's 5 accounts:
#     ZG0790 → Z1####    (1st Zerodha by alphabetical order)
#     ZJ6294 → Z2####
#     DH3747 → D1####    (1st Dhan)
#     DH6847 → D2####
#     GR87DF → G1####    (only Groww)
#
# Empty until `register_accounts` is called; mask_account falls back
# to scalar masking for unregistered codes so demo / test paths that
# never load Connections still produce a sensible mask.
_REGISTRY: dict[str, str] = {}


def register_accounts(accounts) -> None:
    """Rebuild the mask registry from the current account list.

    Called from `Connections.rebuild_from_db()` whenever the broker
    list changes (initial load, operator adds/edits/removes a broker
    via /admin/brokers). Idempotent — re-registering replaces the
    previous mapping atomically.

    Mask shape: 6 chars total, last 4 masked.
      • When the natural first 2 chars are UNIQUE across the account
        list, preserve them as-is:
            ZG0790 → ZG####     (only Zerodha-G account)
            ZJ6294 → ZJ####     (only Zerodha-J account)
            GR87DF → GR####     (only Groww account)
      • When 2+ accounts collide on their first 2 chars (e.g. both
        Dhan accounts start with 'DH'), fall back to the
        ordinal-disambiguator:
            DH3747 → D1####     (1st Dhan by alphabetical order)
            DH6847 → D2####     (2nd Dhan)

    Sort order is the raw account code so ordinal assignment stays
    deterministic across deploys: if you add a new Dhan account
    whose code sorts after the existing two, it becomes D3#### and
    the existing D1 / D2 don't renumber.

    Operator: 'for the masked accounts, only the last 4 chars should
    be masked. for dhan the first 2 chars should show as D1 and D2'.
    """
    global _REGISTRY
    # First pass: bucket by FIRST-2-CHAR prefix to detect collisions.
    by_prefix2: dict[str, list[str]] = {}
    for a in sorted(set(accounts or []) - {"", "TOTAL"}):
        if not a or len(a) < 2:
            continue
        by_prefix2.setdefault(a[:2].upper(), []).append(a)

    new_map: dict[str, str] = {}
    for prefix2, accts in by_prefix2.items():
        if len(accts) == 1:
            # No collision — keep the natural first 2 chars.
            new_map[accts[0]] = f"{prefix2}####"
        else:
            # Collision — use broker-letter + 1-based ordinal so
            # multi-account brokers like the operator's two Dhans
            # stay distinguishable in masked views.
            broker_letter = prefix2[0]
            for i, a in enumerate(accts, 1):
                new_map[a] = f"{broker_letter}{i}####"
    _REGISTRY = new_map


def _scalar_mask(s: str) -> str:
    """Fallback for accounts not in the registry: replace ALL digits
    with `#` (the old default). Letters preserved. Examples:
        'ZG0790' → 'ZG####'
        'GR87DF' → 'GR##DF'
    Used when register_accounts hasn't run yet (e.g. ad-hoc scripts,
    tests, or transient calls during boot before Connections loads).
    """
    return _MASK_RE.sub('#', s)


def mask_account(s: str) -> str:
    """Scalar account-mask. Examples (after register_accounts has run):
        'ZG0790' → 'Z1####'
        'ZJ6294' → 'Z2####'
        'DH3747' → 'D1####'
        'DH6847' → 'D2####'
        'GR87DF' → 'G1####'

    The mask shape is <broker letter><1-based ordinal>#### — operator
    asked for this so the broker is recognisable at a glance AND
    multiple accounts at the same broker don't collapse to the same
    masked string.

    TOTAL is special-cased — it's a virtual aggregate row, not a real
    account, and should pass through unchanged in every masked view."""
    if not s:
        return ""
    if s == "TOTAL":
        return s
    if s in _REGISTRY:
        return _REGISTRY[s]
    return _scalar_mask(s)


def mask_column(col):
    return col.astype(str).map(mask_account)


# Pattern matching the broker-account-code shape used by Zerodha (ZG####,
# ZJ####), Dhan (DH####), and Groww (GR####) — two uppercase letters
# followed by 4-8 alphanumerics. Used by `mask_account_in_text` to
# rewrite account codes embedded inside free-form payload strings.
_ACCT_IN_TEXT_RE = re.compile(r'\b([A-Z]{2}[A-Z0-9]{4,8})\b')


def mask_account_in_text(text: str | None) -> str | None:
    """Mask every broker-account code embedded inside a free-form text
    blob (typically a JSON payload string). Each match is rewritten via
    the canonical `mask_account()` registry so ordinal-aware codes
    (DH3747 → D1####, DH6847 → D2####) survive the substitution.

    Returns the input unchanged when None, empty, or no account-shaped
    tokens are present. Used by /orders/events surfaces to scrub
    payload_json for non-admin viewers."""
    if not text:
        return text
    return _ACCT_IN_TEXT_RE.sub(lambda m: mask_account(m.group(1)), text)


def add_comma_to_df_numbers(df):
    # Format numeric cols with Indian commas
    num_cols = df.select_dtypes(include=["number"]).columns.tolist()
    for col in num_cols:
        df[col] = df[col].apply(add_comma_to_number)
    return df


def add_comma_to_number(x):
    if pd.isna(x):
        return ""
    try:
        num = float(x)
        if abs(num) >= 1000:
            # No decimals for numbers >= 1000
            return format_decimal(num, locale="en_IN", format="#,##,##0")
        else:
            # Max 2 decimal places
            return format_decimal(num, locale="en_IN", format="#,##0.##")
    except Exception:
        return x


def validate_email(email: str) -> bool:
    """Check if email has a valid format."""
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return re.match(pattern, email) is not None


def validate_password_standard(password: str) -> tuple[bool, str]:
    """
    Validate password if not in production.
    Returns:
        (bool, str): (is_valid, message)
    """
    # Read live: `auth.enforce_password_standard` in /admin/settings;
    # YAML `enforce_password_standard` is the boot-time fallback.
    from backend.shared.helpers.settings import get_bool
    if not get_bool("auth.enforce_password_standard",
                    bool(ramboq_deploy.get('enforce_password_standard', False))):
        return True, "Validation skipped in production mode."

    if len(password) < 8:
        return False, "Password must be at least 8 characters long."
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter."
    if not re.search(r"[0-9]", password):
        return False, "Password must contain at least one digit."
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        return False, "Password must contain at least one special character."

    return True, "Password is valid."


def validate_captcha(answer, result):
    try:
        if float(answer) == result:
            return True, "Captcha validated successfully."
        else:
            return False, "Captcha answer is incorrect."
    except ValueError:
        return False, "Please enter a numeric answer for the captcha."


def validate_phone(country_code: str, phone_number: str):
    # Keep only digits in country code

    if not country_code:
        return False, "❌ Phone country code is not selected", None

    phone_pattern = r"^[0-9+\s()]+$"
    if not re.match(phone_pattern, phone_number):
        return False, "❌ Phone number may only contain digits, +, spaces, ( and )", None

    digits_only = re.sub(r"\D", "", phone_number)
    if not (7 <= len(digits_only) <= 15):
        return False, "❌ Phone number must be between 7 and 15 digits", None

    return True, "", digits_only





