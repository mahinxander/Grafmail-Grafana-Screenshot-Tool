#!/usr/bin/env python3
# =============================================================================
# GrafMail - Grafana Dashboard Screenshot Tool
# =============================================================================
# A comprehensive solution for capturing Grafana dashboard screenshots and
# emailing them via SMTP. Designed for both online and offline/air-gapped 
# environments.
#
# Features:
# - Pre-flight checks (env file existence, Grafana reachability)
# - Dual authentication (Service Account Token or Username/Password)
# - Multiple output formats (PNG/PDF)
# - Multiple panel capture with sequential workflow
# - Custom URL parameters (Grafana variables)
# - Flexible email delivery
# - Automatic file cleanup based on retention days
#
# Author: Md Mahin Rahman
# Version: 1.0.0
# =============================================================================

import os
import sys
import time
import logging
import signal
import warnings
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple, Dict
from urllib.parse import urljoin, urlparse, urlencode

# Local module imports
from smtp_sender import SmtpSender

# paramiko for Python-native SFTP (used when REMOTE_COPY_METHOD=paramiko)
try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

# Pillow for merging multiple PNG captures into one PDF
try:
    from PIL import Image as PilImage
    HAS_PIL = True
except ImportError as e:
    HAS_PIL = False
    print(f"ERROR: Missing required dependency: {e}")
    print("Please install dependencies: pip install -r requirements.txt")
    sys.exit(1)

# Third-party imports
try:
    from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, TimeoutError as PlaywrightTimeoutError
    from dotenv import load_dotenv
    import requests
    from dateutil import tz
except ImportError as e:
    print(f"ERROR: Missing required dependency: {e}")
    print("Please install dependencies: pip install -r requirements.txt")
    sys.exit(1)

# =============================================================================
# Configuration & Constants
# =============================================================================

APP_NAME = "GrafMail"
VERSION = "1.0.0"

# Default timeouts (in milliseconds)
DEFAULT_PAGE_TIMEOUT = 60000  # 60 seconds
DEFAULT_NAVIGATION_TIMEOUT = 90000  # 90 seconds
DEFAULT_SPINNER_WAIT_TIMEOUT = 120000  # 120 seconds
DEFAULT_HEARTBEAT_TIMEOUT = 30  # 30 seconds for HTTP requests

# Environment variable names with defaults
ENV_VARS = {
    # Grafana Configuration
    'GRAFANA_URL': None,
    'GRAFANA_DASHBOARD_UID': None,
    'GRAFANA_DASHBOARD_SLUG': None,
    
    # Panel Configuration (supports multiple panels)
    'GRAFANA_PANEL_IDS': None,
    'GRAFANA_PANEL_ID': None,   # Legacy single panel
    
    # Custom URL Parameters
    'GRAFANA_CUSTOM_PARAMS': None,
    
    # Capture Workflow
    'CAPTURE_VIEWPORT': 'true',
    'CAPTURE_PANELS': 'true',
    'PANEL_LOAD_WAIT': '3',
    'PANEL_PARAM_TYPE': 'viewPanel',
    
    # Sidebar Configuration
    'HIDE_SIDEBAR': 'true',  # Hide Grafana sidebar in screenshots
    
    # TLS/SSL Verification
    'GRAFANA_IGNORE_TLS_ERRORS': 'true',  # Ignore TLS cert errors
    
    # Authentication
    'GRAFANA_AUTH_METHOD': 'password',
    'GRAFANA_SERVICE_TOKEN': None,
    'GRAFANA_USERNAME': None,
    'GRAFANA_PASSWORD': None,
    
    # Screenshot Configuration
    'SCREENSHOT_WIDTH': '1920',
    'SCREENSHOT_HEIGHT': '1080',
    'SCREENSHOT_FORMAT': 'png',
    'SCREENSHOT_FULL_PAGE': 'false',
    'CAPTURE_DIR': '/app/captures',
    
    # Dashboard Rendering Options
    'DASHBOARD_REFRESH': None,
    'DASHBOARD_TIME_FROM': 'now-24h',
    'DASHBOARD_TIME_TO': 'now',
    'DASHBOARD_THEME': 'dark',
    'DASHBOARD_KIOSK': 'true',
    
    # Delivery Mode Configuration
    # SMTP_INTERNAL = capture + send email
    # FILE_ONLY     = capture only, save to CAPTURE_DIR, no email
    'DELIVERY_MODE': 'SMTP_INTERNAL',
    
    # SMTP Configuration (used when DELIVERY_MODE=SMTP_INTERNAL)
    'SMTP_HOST': None,
    'SMTP_PORT': '587',
    'SMTP_USER': None,              # Optional — omit for no-auth relay (port 25)
    'SMTP_PASSWORD': None,          # Optional — omit for no-auth relay (port 25)
    'SMTP_FROM': None,
    'SMTP_TO': None,                # Comma-separated list of recipients
    'SMTP_CC': None,                # CC recipients (comma-separated)
    'SMTP_BCC': None,               # BCC recipients (comma-separated)
    'SMTP_USE_TLS': 'true',
    'SMTP_USE_SSL': 'false',        # Use SSL (port 465) instead of STARTTLS
    'SMTP_SUBJECT': 'Grafana Dashboard Report',
    'EMAIL_BODY_MESSAGE': None,     # Custom email body (overrides default)
    'NO_IMAGES_ACTION': 'notify',   # What to do if no images: notify, skip, fail
    
    # Optional Remote Copy (FILE_ONLY mode only)
    'SEND_IMG_TO_REMOTE': 'false',  # Copy captures to a remote path after capture
    'REMOTE_COPY_PATH': None,       # Destination path, e.g. user@host:/remote/dir or /local/dir
    'REMOTE_COPY_METHOD': 'paramiko',  # Transfer method: paramiko or local
    'SSH_HOST_KEY_POLICY': 'warn',  # SSH host key policy: reject, warn, or auto
    'SSH_KEY_PATH': None,           # Path to SSH private key (inside container), e.g. /home/appuser/.ssh/id_rsa
    'SSH_PASSWORD': None,           # SSH password for password-based auth (if no key)
    'SSH_PORT': '22',               # SSH port for paramiko connections
    
    # File Cleanup
    'FILE_RETENTION_DAYS': '7',     # Delete files older than X days (0 = disabled)
    'CLEANUP_ON_START': 'true',     # Run cleanup before capture
    
    # Retry & Timeout
    'CAPTURE_RETRY_COUNT': '2',     # Retry failed screenshot captures
    'CAPTURE_TIMEOUT': '120',       # Per-dashboard/panel capture timeout (seconds)
    
    # Docker Resource Limits (read by run_report.sh, not used in Python)
    'DOCKER_MEMORY': '1g',          # Container memory limit
    'DOCKER_CPUS': '1.5',           # Container CPU limit
    
    # Advanced Options
    'SPINNER_SELECTORS': None,
    'WAIT_AFTER_LOAD': '5',
    'DEBUG_MODE': 'false',
}

# =============================================================================
# Logging Setup
# =============================================================================

def setup_logging(debug: bool = False) -> logging.Logger:
    """Configure and return the application logger."""
    log_level = logging.DEBUG if debug else logging.INFO
    
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(log_level)
    
    if logger.handlers:
        return logger
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    
    formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(formatter)
    
    logger.addHandler(console_handler)
    
    return logger


logger = setup_logging()


# =============================================================================
# Configuration Loader
# =============================================================================

class Config:
    """Configuration manager that loads settings from environment variables."""
    
    def __init__(self, env_file: Optional[str] = None):
        self._config = {}
        self._load_config(env_file)
    
    def _load_config(self, env_file: Optional[str]) -> None:
        if env_file:
            env_path = Path(env_file)
            if env_path.exists():
                load_dotenv(env_path)
                logger.info(f"Loaded configuration from: {env_path}")
            else:
                logger.warning(f"Environment file not found: {env_path}")
        
        for key, default in ENV_VARS.items():
            value = os.environ.get(key, default)
            self._config[key] = value
    
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._config.get(key, default)
    
    def get_int(self, key: str, default: int = 0) -> int:
        value = self._config.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default
    
    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self._config.get(key)
        if value is None:
            return default
        return value.lower() in ('true', '1', 'yes', 'on')
    
    def get_list(self, key: str, separator: str = ',') -> List[str]:
        value = self._config.get(key)
        if not value:
            return []
        return [item.strip() for item in value.split(separator) if item.strip()]
    
    def get_required(self, key: str) -> str:
        value = self._config.get(key)
        if not value:
            raise ValueError(f"Required configuration missing: {key}")
        return value
    
    def get_panel_ids(self) -> List[Dict[str, str]]:
        """Parse panel IDs from configuration."""
        panels = []
        
        panel_ids_str = self.get('GRAFANA_PANEL_IDS')
        if panel_ids_str:
            for item in panel_ids_str.split(','):
                item = item.strip()
                if not item:
                    continue
                    
                if ':' in item:
                    param_type, panel_id = item.split(':', 1)
                    panels.append({
                        'id': panel_id.strip(),
                        'param_type': param_type.strip()
                    })
                else:
                    default_param = self.get('PANEL_PARAM_TYPE', 'viewPanel')
                    panels.append({
                        'id': item,
                        'param_type': default_param
                    })
        
        elif self.get('GRAFANA_PANEL_ID'):
            default_param = self.get('PANEL_PARAM_TYPE', 'viewPanel')
            panels.append({
                'id': self.get('GRAFANA_PANEL_ID'),
                'param_type': default_param
            })
        
        return panels
    
    def get_custom_params(self) -> Dict[str, str]:
        """Parse custom URL parameters from configuration."""
        params = {}
        custom_params_str = self.get('GRAFANA_CUSTOM_PARAMS')
        
        if not custom_params_str:
            return params
        
        items = []
        for part in custom_params_str.split('&'):
            if ',' in part and '=' not in part.split(',')[0]:
                items.extend(part.split(','))
            else:
                items.append(part)
        
        for item in items:
            item = item.strip()
            if '=' in item:
                key, value = item.split('=', 1)
                params[key.strip()] = value.strip()
        
        return params
    
    def validate(self) -> Tuple[bool, List[str]]:
        """Validate that all required configuration is present."""
        errors = []
        
        if not self.get('GRAFANA_URL'):
            errors.append("GRAFANA_URL is required")
        
        auth_method = self.get('GRAFANA_AUTH_METHOD', 'password').lower()
        if auth_method == 'token':
            if not self.get('GRAFANA_SERVICE_TOKEN'):
                errors.append("GRAFANA_SERVICE_TOKEN is required when using token authentication")
        elif auth_method == 'password':
            if not self.get('GRAFANA_USERNAME') or not self.get('GRAFANA_PASSWORD'):
                errors.append("GRAFANA_USERNAME and GRAFANA_PASSWORD are required when using password authentication")
        else:
            errors.append(f"Invalid GRAFANA_AUTH_METHOD: {auth_method}. Use 'token' or 'password'")
        
        # Validate delivery mode configuration
        delivery_mode = self.get('DELIVERY_MODE', 'SMTP_INTERNAL').upper()
        
        if delivery_mode == 'SMTP_INTERNAL':
            required_smtp = ['SMTP_HOST', 'SMTP_FROM', 'SMTP_TO']
            for key in required_smtp:
                if not self.get(key):
                    errors.append(f"{key} is required when DELIVERY_MODE is SMTP_INTERNAL")
        
        elif delivery_mode == 'FILE_ONLY':
            if self.get_bool('SEND_IMG_TO_REMOTE'):
                if not self.get('REMOTE_COPY_PATH'):
                    errors.append("REMOTE_COPY_PATH is required when SEND_IMG_TO_REMOTE is enabled")
                method = self.get('REMOTE_COPY_METHOD', 'paramiko').lower()
                if method not in ('paramiko', 'local'):
                    errors.append(f"Invalid REMOTE_COPY_METHOD: {method}. Use 'paramiko' or 'local'")
                if method == 'paramiko' and not HAS_PARAMIKO:
                    errors.append("REMOTE_COPY_METHOD=paramiko but paramiko is not installed")
        
        else:
            errors.append(f"Invalid DELIVERY_MODE: {delivery_mode}. Use 'SMTP_INTERNAL' or 'FILE_ONLY'")
        
        # Validate NO_IMAGES_ACTION
        no_img = self.get('NO_IMAGES_ACTION', 'notify').lower()
        if no_img not in ('notify', 'skip', 'fail'):
            errors.append(f"Invalid NO_IMAGES_ACTION: {no_img}. Use 'notify', 'skip', or 'fail'")
        
        return len(errors) == 0, errors


# =============================================================================
# File Cleanup Manager
# =============================================================================

class FileCleanup:
    """Handles cleanup of old screenshot files."""
    
    def __init__(self, config: Config):
        self.config = config
        self.capture_dir = Path(config.get('CAPTURE_DIR', '/app/captures'))
        self.retention_days = config.get_int('FILE_RETENTION_DAYS', 7)
        self.cleanup_on_start = config.get_bool('CLEANUP_ON_START')
    
    def run_cleanup(self) -> Tuple[int, int]:
        """
        Delete files older than retention_days. Recurses into subdirectories
        (dashboard UID folders) and removes empty subdirs after cleanup.
        
        Returns:
            Tuple of (files_deleted, bytes_freed)
        """
        if self.retention_days <= 0:
            logger.info("File cleanup is disabled (FILE_RETENTION_DAYS=0)")
            return 0, 0
        
        if not self.capture_dir.exists():
            logger.debug(f"Capture directory does not exist: {self.capture_dir}")
            return 0, 0
        
        cutoff_date = datetime.now() - timedelta(days=self.retention_days)
        files_deleted = 0
        bytes_freed = 0
        
        logger.info(f"Cleaning up files older than {self.retention_days} days (before {cutoff_date.strftime('%Y-%m-%d %H:%M')})")
        
        # Recurse into all subdirectories (dashboard UID folders)
        for file_path in self.capture_dir.rglob('*'):
            if file_path.is_file():
                try:
                    mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                    if mtime < cutoff_date:
                        file_size = file_path.stat().st_size
                        file_path.unlink()
                        files_deleted += 1
                        bytes_freed += file_size
                        logger.debug(f"Deleted: {file_path} ({file_size} bytes, modified {mtime})")
                except Exception as e:
                    logger.warning(f"Failed to delete {file_path}: {str(e)}")
        
        # Remove empty subdirectories
        for dir_path in sorted(self.capture_dir.rglob('*'), reverse=True):
            if dir_path.is_dir():
                try:
                    if not any(dir_path.iterdir()):
                        dir_path.rmdir()
                        logger.debug(f"Removed empty directory: {dir_path}")
                except Exception as e:
                    logger.warning(f"Failed to remove directory {dir_path}: {str(e)}")
        
        if files_deleted > 0:
            mb_freed = bytes_freed / (1024 * 1024)
            logger.info(f"Cleanup complete: {files_deleted} file(s) deleted, {mb_freed:.2f} MB freed")
        else:
            logger.info("No files to clean up")
        
        return files_deleted, bytes_freed


# =============================================================================
# Pre-flight Checks
# =============================================================================

class PreFlightChecker:
    """Performs pre-flight validation checks before executing the main workflow."""
    
    def __init__(self, config: Config):
        self.config = config
        self.results = []
    
    def check_grafana_reachable(self) -> Tuple[bool, str]:
        """Check if the Grafana server is reachable via heartbeat."""
        grafana_url = self.config.get('GRAFANA_URL')
        if not grafana_url:
            return False, "GRAFANA_URL not configured"
        
        parsed = urlparse(grafana_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        health_url = urljoin(base_url, '/api/health')
        
        logger.info(f"Performing heartbeat check: {health_url}")
        
        try:
            timeout = DEFAULT_HEARTBEAT_TIMEOUT
            ignore_tls = self.config.get_bool('GRAFANA_IGNORE_TLS_ERRORS', True)
            response = requests.get(
                health_url,
                timeout=timeout,
                verify=not ignore_tls,
                headers={'Accept': 'application/json'}
            )
            
            if response.status_code == 200:
                data = response.json()
                version = data.get('version', 'unknown')
                return True, f"Grafana reachable (version: {version})"
            else:
                return False, f"Grafana returned status code: {response.status_code}"
                
        except requests.exceptions.Timeout:
            return False, f"Connection timeout after {timeout}s"
        except requests.exceptions.ConnectionError as e:
            return False, f"Connection error: {str(e)}"
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"
    
    def check_capture_directory(self) -> Tuple[bool, str]:
        """Check if the capture directory exists and is writable.
        
        Attempts to create the directory if missing, and verifies write
        access by actually writing a test file (more reliable than os.access
        when UID/GID mapping is involved in Docker containers).
        """
        capture_dir = Path(self.config.get('CAPTURE_DIR', '/app/captures'))
        
        if not capture_dir.exists():
            try:
                capture_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return False, f"Failed to create capture directory: {str(e)}"
        
        # Verify writable by creating a test file 
        test_file = capture_dir / '.write_test'
        try:
            test_file.touch()
            test_file.unlink()
            return True, (
                f"Capture directory ready: {capture_dir} "
                f"(uid={os.getuid()}, gid={os.getgid()})"
            )
        except PermissionError:
            return False, (
                f"Capture directory not writable: {capture_dir}\n"
                f"  Container UID={os.getuid()}, GID={os.getgid()}\n"
                f"  Ensure run_report.sh passes --user $(id -u):$(id -g) to docker run,\n"
                f"  or run: chmod g+w {capture_dir} on the host."
            )
        except Exception as e:
            return False, f"Capture directory write test failed: {str(e)}"
    
    def run_all_checks(self) -> bool:
        """Run all pre-flight checks and report results."""
        logger.info("=" * 60)
        logger.info("PRE-FLIGHT CHECKS")
        logger.info("=" * 60)
        
        delivery_mode = self.config.get('DELIVERY_MODE', 'SMTP_INTERNAL').upper()
        logger.info(f"Delivery mode: {delivery_mode}")
        
        checks = [
            ("Configuration Validation", self._validate_config),
            ("Capture Directory", self.check_capture_directory),
            ("Grafana Heartbeat", self.check_grafana_reachable),
        ]
        
        all_passed = True
        
        for check_name, check_func in checks:
            try:
                passed, message = check_func()
                status = "✓ PASS" if passed else "✗ FAIL"
                logger.info(f"{status} | {check_name}: {message}")
                self.results.append((check_name, passed, message))
                if not passed:
                    all_passed = False
            except Exception as e:
                logger.error(f"✗ FAIL | {check_name}: Unexpected error - {str(e)}")
                self.results.append((check_name, False, str(e)))
                all_passed = False
        
        logger.info("=" * 60)
        
        if all_passed:
            logger.info("All pre-flight checks passed!")
        else:
            logger.error("One or more pre-flight checks failed!")
        
        return all_passed
    
    def _validate_config(self) -> Tuple[bool, str]:
        is_valid, errors = self.config.validate()
        if is_valid:
            return True, "All required configuration present"
        else:
            return False, "; ".join(errors)


# =============================================================================
# Grafana Screenshot Capture
# =============================================================================

class GrafanaScreenshot:
    """Handles Grafana dashboard screenshot capture using Playwright."""
    
    DEFAULT_SPINNER_SELECTORS = [
        'div.dashboard-loading',
        'div.loader-container',
        'div.loading-row',
        '.loading-indicator',
        '[data-testid="data-testid Spinner"]',
        'span.spinner',
        'div.spinner',
        'svg.grafana-app',
        '.panel-loading',
        'div[data-testid="data-testid Panel header"] svg',
        '.panel-content .loading',
        '[aria-label="Loading"]',
    ]
    
    # CSS to hide Grafana sidebar
    SIDEBAR_HIDE_CSS = """
    /* Hide the main sidebar */
    .sidemenu,
    [class*="sidemenu"],
    [data-testid="data-testid Sidemenu"],
    nav[class*="sidemenu"],
    .css-1i23d5n-sidemenu,
    [aria-label="Side menu"],
    [aria-label="SideMenu"] {
        display: none !important;
    }
    
    /* Adjust main content area to fill full width */
    .main-view,
    [class*="main-view"],
    .dashboard-container,
    [class*="dashboard-container"],
    .css-1i23d5n-main-view,
    .scrollbar-view {
        margin-left: 0 !important;
        padding-left: 0 !important;
        left: 0 !important;
    }
    
    /* Hide sidebar toggle button */
    [class*="sidemenu-toggle"],
    [aria-label="Toggle side menu"],
    .css-1s45uyp {
        display: none !important;
    }
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.playwright = None
        self.captured_files: List[Path] = []
        
        self.debug = config.get_bool('DEBUG_MODE')
        if self.debug:
            logger.setLevel(logging.DEBUG)
        
        self.hide_sidebar = config.get_bool('HIDE_SIDEBAR')
    
    def _build_dashboard_url(self, panel_config: Optional[Dict[str, str]] = None) -> str:
        """Build the complete dashboard URL with query parameters."""
        base_url = self.config.get_required('GRAFANA_URL').rstrip('/')
        
        uid = self.config.get('GRAFANA_DASHBOARD_UID')
        slug = self.config.get('GRAFANA_DASHBOARD_SLUG')
        
        if uid:
            path = f"/d/{uid}"
            if slug:
                path += f"/{slug}"
        elif slug:
            path = f"/dashboard/db/{slug}"
        else:
            path = ""
        
        params = []
        
        time_from = self.config.get('DASHBOARD_TIME_FROM')
        time_to = self.config.get('DASHBOARD_TIME_TO')
        if time_from:
            params.append(f"from={time_from}")
        if time_to:
            params.append(f"to={time_to}")
        
        if panel_config:
            param_type = panel_config.get('param_type', 'viewPanel')
            panel_id = panel_config.get('id')
            if panel_id:
                params.append(f"{param_type}={panel_id}")
                if param_type == 'panelId':
                    params.append("fullscreen=true")
        
        theme = self.config.get('DASHBOARD_THEME', 'dark')
        params.append(f"theme={theme}")
        
        # Only add kiosk for viewport (not individual panels)
        if not panel_config:
            kiosk = self.config.get('DASHBOARD_KIOSK', 'true')
            if kiosk.lower() != 'false':
                # Use 'tv' kiosk mode for cleaner view without sidebar
                params.append("kiosk=tv")
        
        refresh = self.config.get('DASHBOARD_REFRESH')
        if refresh:
            params.append(f"refresh={refresh}")
        
        custom_params = self.config.get_custom_params()
        for key, value in custom_params.items():
            params.append(f"{key}={value}")
        
        url = f"{base_url}{path}"
        if params:
            url += "?" + "&".join(params)
        
        return url
    
    def _get_spinner_selectors(self) -> List[str]:
        custom_selectors = self.config.get('SPINNER_SELECTORS')
        if custom_selectors:
            return [s.strip() for s in custom_selectors.split(',') if s.strip()]
        return self.DEFAULT_SPINNER_SELECTORS
    
    def _inject_sidebar_hide_css(self):
        """Inject CSS to hide the sidebar."""
        if self.hide_sidebar:
            try:
                self.page.add_style_tag(content=self.SIDEBAR_HIDE_CSS)
                logger.debug("Injected CSS to hide sidebar")
            except Exception as e:
                logger.warning(f"Failed to inject sidebar CSS: {str(e)}")
    
    def _wait_for_dashboard_load(self, timeout: int = DEFAULT_SPINNER_WAIT_TIMEOUT) -> bool:
        """Wait for Grafana dashboard to fully load by detecting loading spinners."""
        logger.info("Waiting for dashboard/panel to fully load...")
        
        selectors = self._get_spinner_selectors()
        start_time = time.time()
        timeout_seconds = timeout / 1000
        
        time.sleep(2)
        
        while (time.time() - start_time) < timeout_seconds:
            spinners_found = False
            
            for selector in selectors:
                try:
                    spinner = self.page.query_selector(selector)
                    if spinner:
                        is_visible = spinner.is_visible()
                        if is_visible:
                            spinners_found = True
                            logger.debug(f"Spinner found: {selector}")
                            break
                except Exception:
                    pass
            
            if not spinners_found:
                logger.info("All loading spinners have disappeared")
                
                # Inject CSS to hide sidebar after content loads
                if self.hide_sidebar:
                    self._inject_sidebar_hide_css()
                    time.sleep(0.5)  # Brief wait for CSS to take effect
                
                wait_after = min(self.config.get_int('WAIT_AFTER_LOAD', 5), 300)
                if wait_after > 0:
                    logger.info(f"Waiting {wait_after}s for charts to render...")
                    time.sleep(wait_after)
                
                return True
            
            time.sleep(0.5)
        
        logger.warning(f"Timeout waiting for dashboard load after {timeout_seconds}s")
        return False
    
    def _perform_login(self) -> bool:
        """Perform Grafana login using configured authentication method."""
        auth_method = self.config.get('GRAFANA_AUTH_METHOD', 'password').lower()
        
        if auth_method == 'token':
            return self._login_with_token()
        else:
            return self._login_with_password()
    
    def _login_with_token(self) -> bool:
        """Authenticate using Service Account Token via API headers."""
        token = self.config.get('GRAFANA_SERVICE_TOKEN')
        if not token:
            logger.error("Service token not configured")
            return False
        
        logger.info("Authenticating using Service Account Token")
        
        self.context.set_extra_http_headers({
            'Authorization': f'Bearer {token}'
        })
        
        return True
    
    def _login_with_password(self) -> bool:
        """Authenticate using Username/Password via UI login form."""
        username = self.config.get('GRAFANA_USERNAME')
        password = self.config.get('GRAFANA_PASSWORD')
        
        if not username or not password:
            logger.error("Username or password not configured")
            return False
        
        logger.info(f"Authenticating as user: {username}")
        
        base_url = self.config.get_required('GRAFANA_URL').rstrip('/')
        login_url = f"{base_url}/login"
        
        try:
            self.page.goto(login_url, wait_until='networkidle', timeout=DEFAULT_NAVIGATION_TIMEOUT)
        except PlaywrightTimeoutError:
            logger.warning("Timeout navigating to login page, continuing anyway...")
        
        time.sleep(1)
        
        username_selectors = [
            'input[name="user"]',
            'input[id="user"]',
            'input[aria-label="Username input field"]',
            'input[placeholder*="username" i]',
            'input[placeholder*="email" i]',
        ]
        
        username_input = None
        for selector in username_selectors:
            try:
                username_input = self.page.wait_for_selector(selector, timeout=5000)
                if username_input:
                    break
            except PlaywrightTimeoutError:
                continue
        
        if not username_input:
            logger.error("Could not find username input field")
            return False
        
        username_input.fill(username)
        
        password_selectors = [
            'input[name="password"]',
            'input[id="password"]',
            'input[type="password"]',
            'input[aria-label="Password input field"]',
        ]
        
        password_input = None
        for selector in password_selectors:
            try:
                password_input = self.page.wait_for_selector(selector, timeout=5000)
                if password_input:
                    break
            except PlaywrightTimeoutError:
                continue
        
        if not password_input:
            logger.error("Could not find password input field")
            return False
        
        password_input.fill(password)
        
        submit_selectors = [
            'button[type="submit"]',
            'button[aria-label="Login button"]',
            'button:has-text("Log in")',
            'button:has-text("Sign in")',
        ]
        
        for selector in submit_selectors:
            try:
                submit_btn = self.page.wait_for_selector(selector, timeout=5000)
                if submit_btn:
                    try:
                        with self.page.expect_navigation(timeout=30000, wait_until='networkidle'):
                            submit_btn.click()
                        logger.debug("Navigation completed after login click")
                    except PlaywrightTimeoutError:
                        logger.debug("Navigation timeout after login click, checking page state...")
                    break
            except PlaywrightTimeoutError:
                continue
        
        time.sleep(2)
        
        # Only check for login errors if we are still on the login page
        if '/login' in self.page.url:
            error_selectors = [
                '.alert-error',
                '.login-error-message',
                '[role="alert"]',
                '.alert-danger',
                '[data-testid="data-testid Alert error"]',
            ]
            
            for selector in error_selectors:
                try:
                    error_el = self.page.query_selector(selector)
                    if error_el and error_el.is_visible():
                        error_text = error_el.text_content().strip()
                        logger.error(f"Login failed: {error_text}")
                        return False
                except Exception:
                    pass
        
        logger.info("Login successful")
        return True
    
    def _capture_screenshot(self, name_suffix: str = "") -> Optional[Path]:
        """Capture a screenshot of the current page.
        
        Screenshots are saved in a subdirectory named after the dashboard UID:
          captures/{GRAFANA_DASHBOARD_UID}/grafana_{uid}_{suffix}_{timestamp}.png
        """
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            base_capture_dir = Path(self.config.get('CAPTURE_DIR', '/app/captures'))
            format_type = self.config.get('SCREENSHOT_FORMAT', 'png').lower()
            uid = self.config.get('GRAFANA_DASHBOARD_UID', 'dashboard')
            
            # Create subdirectory for this dashboard UID
            capture_dir = base_capture_dir / uid
            capture_dir.mkdir(parents=True, exist_ok=True)
            
            if name_suffix:
                filename = f"grafana_{uid}_{name_suffix}_{timestamp}.{format_type}"
            else:
                filename = f"grafana_{uid}_{timestamp}.{format_type}"
            
            output_path = capture_dir / filename
            
            logger.info(f"Capturing screenshot to: {output_path}")
            
            full_page = self.config.get_bool('SCREENSHOT_FULL_PAGE', False)
            
            if format_type == 'pdf':
                png_path = capture_dir / filename.replace('.pdf', '.png')
                self.page.screenshot(
                    path=str(png_path),
                    full_page=full_page,
                    type='png'
                )
                logger.info(f"Screenshot (pre-PDF) saved: {png_path}")
                return png_path
            else:
                self.page.screenshot(
                    path=str(output_path),
                    full_page=full_page,
                    type='png'
                )
            
            logger.info(f"Screenshot saved successfully: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"Failed to capture screenshot: {str(e)}")
            return None
    
    def _navigate_to_url(self, url: str) -> bool:
        """Navigate to a URL with retry logic."""
        logger.info(f"Navigating to: {url}")
        
        max_retries = 3
        nav_success = False
        last_error = None
        
        for attempt in range(1, max_retries + 1):
            try:
                self.page.goto(url, wait_until='networkidle', timeout=DEFAULT_NAVIGATION_TIMEOUT)
                nav_success = True
                break
            except PlaywrightTimeoutError:
                logger.warning(f"Page navigation timeout (attempt {attempt}/{max_retries}), proceeding...")
                nav_success = True
                break
            except Exception as e:
                last_error = e
                logger.warning(f"Navigation error (attempt {attempt}/{max_retries}): {str(e)}")
                if attempt < max_retries:
                    time.sleep(2)
        
        if not nav_success and last_error:
            logger.error(f"Navigation failed after {max_retries} attempts: {last_error}")
            return False
        
        return True
    
    def _capture_with_retry(self, name_suffix: str = "") -> Optional[Path]:
        """Capture a screenshot with retry logic."""
        max_retries = self.config.get_int('CAPTURE_RETRY_COUNT', 2)
        
        for attempt in range(1, max_retries + 1):
            result = self._capture_screenshot(name_suffix=name_suffix)
            if result:
                return result
            if attempt < max_retries:
                logger.warning(f"Capture failed (attempt {attempt}/{max_retries}), retrying in 3s...")
                time.sleep(3)
        
        logger.error(f"Capture failed after {max_retries} attempts for: {name_suffix}")
        return None
    
    def capture_all(self) -> List[Path]:
        """Capture screenshots based on configuration."""
        logger.info("=" * 60)
        logger.info("STARTING SCREENSHOT CAPTURE")
        logger.info("=" * 60)
        
        try:
            self.playwright = sync_playwright().start()
            
            logger.info("Launching Chromium browser...")
            self.browser = self.playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    # Memory management flags
                    '--js-flags=--max-old-space-size=512',
                    '--disable-extensions',
                    '--disable-background-networking',
                ]
            )
            
            width = self.config.get_int('SCREENSHOT_WIDTH', 1920)
            height = self.config.get_int('SCREENSHOT_HEIGHT', 1080)
            
            ignore_tls = self.config.get_bool('GRAFANA_IGNORE_TLS_ERRORS', True)
            self.context = self.browser.new_context(
                viewport={'width': width, 'height': height},
                ignore_https_errors=ignore_tls,
            )
            
            self.page = self.context.new_page()
            
            capture_timeout_sec = min(self.config.get_int('CAPTURE_TIMEOUT', 120), 600)
            page_timeout_ms = capture_timeout_sec * 1000
            self.page.set_default_timeout(page_timeout_ms)
            
            if not self._perform_login():
                raise Exception("Authentication failed")
            
            time.sleep(1)
            logger.debug("Session stabilized after login")
            
            panels = self.config.get_panel_ids()
            capture_viewport = self.config.get_bool('CAPTURE_VIEWPORT')
            capture_panels = self.config.get_bool('CAPTURE_PANELS')
            panel_wait = self.config.get_int('PANEL_LOAD_WAIT', 3)
            
            logger.info(f"Capture configuration: viewport={capture_viewport}, panels={capture_panels}, panel_count={len(panels)}")
            logger.info(f"Capture timeout: {capture_timeout_sec}s | Retry count: {self.config.get_int('CAPTURE_RETRY_COUNT', 2)}")
            logger.info(f"Sidebar hiding: {'enabled' if self.hide_sidebar else 'disabled'}")
            
            # Step 1: Capture full dashboard viewport
            if capture_viewport:
                logger.info("-" * 40)
                logger.info("Capturing full dashboard viewport...")
                logger.info("-" * 40)
                
                viewport_url = self._build_dashboard_url(panel_config=None)
                
                if self._navigate_to_url(viewport_url):
                    if self._wait_for_dashboard_load():
                        screenshot_path = self._capture_with_retry(name_suffix="viewport")
                        if screenshot_path:
                            self.captured_files.append(screenshot_path)
                    else:
                        logger.warning("Dashboard may not be fully loaded, capturing anyway...")
                        screenshot_path = self._capture_with_retry(name_suffix="viewport")
                        if screenshot_path:
                            self.captured_files.append(screenshot_path)
            
            # Step 2: Capture individual panels
            if capture_panels and panels:
                logger.info("-" * 40)
                logger.info(f"Capturing {len(panels)} individual panel(s)...")
                logger.info("-" * 40)
                
                for idx, panel in enumerate(panels, 1):
                    panel_id = panel['id']
                    param_type = panel['param_type']
                    
                    logger.info(f"\n[{idx}/{len(panels)}] Capturing panel: {param_type}={panel_id}")
                    
                    panel_url = self._build_dashboard_url(panel_config=panel)
                    
                    if self._navigate_to_url(panel_url):
                        if self._wait_for_dashboard_load():
                            screenshot_path = self._capture_with_retry(name_suffix=f"panel_{param_type}_{panel_id}")
                            if screenshot_path:
                                self.captured_files.append(screenshot_path)
                        else:
                            logger.warning(f"Panel {panel_id} may not be fully loaded, capturing anyway...")
                            screenshot_path = self._capture_with_retry(name_suffix=f"panel_{param_type}_{panel_id}")
                            if screenshot_path:
                                self.captured_files.append(screenshot_path)
                    
                    if idx < len(panels):
                        logger.debug(f"Waiting {panel_wait}s before next panel...")
                        time.sleep(panel_wait)
            
            logger.info("=" * 60)
            logger.info(f"CAPTURE COMPLETE: {len(self.captured_files)} screenshot(s) captured")
            logger.info("=" * 60)
            
            for path in self.captured_files:
                logger.info(f"  - {path}")
            
            return self.captured_files
            
        except Exception as e:
            logger.error(f"Screenshot capture failed: {str(e)}")
            if self.debug:
                logger.exception("Screenshot capture failed")
            return self.captured_files
        
        finally:
            self._cleanup()
    
    def _cleanup(self):
        """Clean up browser resources."""
        try:
            if self.page:
                self.page.close()
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception as e:
            logger.warning(f"Error during cleanup: {str(e)}")


# =============================================================================
# Main Application
# =============================================================================

class Application:
    """Main application orchestrator.
    
    Supports two delivery modes:
      SMTP_INTERNAL — capture screenshots + send email via SMTP (all in Docker)
      FILE_ONLY     — capture screenshots only, save to CAPTURE_DIR, exit cleanly
    """
    
    def __init__(self, env_file: Optional[str] = None):
        self.config = Config(env_file)
        self.preflight = PreFlightChecker(self.config)
        self.screenshot = GrafanaScreenshot(self.config)
        self.cleanup = FileCleanup(self.config)
        self.delivery_mode = self.config.get('DELIVERY_MODE', 'SMTP_INTERNAL').upper()
        
        # Only create SMTP sender if we are in SMTP_INTERNAL mode
        self.smtp_sender = None
        if self.delivery_mode == 'SMTP_INTERNAL':
            self.smtp_sender = SmtpSender(self.config)
    
    def run(self) -> int:
        """Execute the main application workflow."""
        logger.info("=" * 60)
        logger.info(f"{APP_NAME} v{VERSION}")
        logger.info(f"Delivery Mode: {self.delivery_mode}")
        logger.info("=" * 60)
        
        # Run pre-flight checks
        if not self.preflight.run_all_checks():
            logger.error("Pre-flight checks failed, aborting")
            return 1
        
        # Run file cleanup if enabled
        if self.config.get_bool('CLEANUP_ON_START'):
            logger.info("-" * 40)
            self.cleanup.run_cleanup()
            logger.info("-" * 40)
        
        # Capture screenshots
        captured_files = self.screenshot.capture_all()
        
        # -----------------------------------------------------------------
        # PDF Merge: if format=pdf and multiple files, merge into one PDF
        # -----------------------------------------------------------------
        format_type = self.config.get('SCREENSHOT_FORMAT', 'png').lower()
        if format_type == 'pdf' and len(captured_files) >= 1:
            captured_files = self._merge_pdfs(captured_files)
        
        # Handle no-images scenario based on NO_IMAGES_ACTION
        if not captured_files:
            no_images_action = self.config.get('NO_IMAGES_ACTION', 'notify').lower()
            
            if no_images_action == 'notify' and self.delivery_mode == 'SMTP_INTERNAL' and self.smtp_sender:
                logger.warning("No screenshots captured — sending notification email")
                success, message = self.smtp_sender.send_no_images_notification()
                if not success:
                    logger.error(f"Notification email failed: {message}")
                return 2
            elif no_images_action == 'skip':
                logger.info("No screenshots captured — exiting gracefully (NO_IMAGES_ACTION=skip)")
                return 0
            else:
                logger.error("No screenshots were captured")
                return 2
        
        # -----------------------------------------------------------------
        # Delivery Mode A — SMTP_INTERNAL: send email from inside Docker
        # -----------------------------------------------------------------
        if self.delivery_mode == 'SMTP_INTERNAL':
            success, message = self.smtp_sender.send(captured_files)
            if not success:
                logger.error(f"Email send failed: {message}")
                return 3
            logger.info(f"Email result: {message}")
        
        # -----------------------------------------------------------------
        # Delivery Mode B — FILE_ONLY: save to CAPTURE_DIR, no email
        # -----------------------------------------------------------------
        elif self.delivery_mode == 'FILE_ONLY':
            logger.info("=" * 60)
            logger.info("FILE_ONLY MODE — Screenshots saved, no email sent")
            logger.info("=" * 60)
            for path in captured_files:
                logger.info(f"  Saved: {path}")
            
            # Optional: copy files to a remote/secondary path
            if self.config.get_bool('SEND_IMG_TO_REMOTE'):
                self._remote_copy(captured_files)
        
        else:
            logger.error(f"Unknown DELIVERY_MODE: {self.delivery_mode}")
            return 4
        
        logger.info("=" * 60)
        logger.info("COMPLETED SUCCESSFULLY")
        logger.info("=" * 60)
        
        return 0
    
    def _merge_pdfs(self, captured_files: List[Path]) -> List[Path]:
        """Convert PNG screenshots into a single PDF — one image per page.
        Each page is exactly the size of the image. No scaling, no margins.
        """
        if not HAS_PIL:
            logger.warning("Pillow not available — keeping individual PNG files.")
            return captured_files

        if not captured_files:
            return captured_files

        logger.info(f"Converting {len(captured_files)} PNG(s) into a single PDF...")

        try:

            first_file = captured_files[0]
            uid = self.config.get('GRAFANA_DASHBOARD_UID', 'dashboard')
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            merged_path = first_file.parent / f"grafana_{uid}_report_{timestamp}.pdf"

            images = []
            for png_path in captured_files:
                if png_path.exists():
                    images.append(PilImage.open(png_path).convert('RGB'))
                    logger.debug(f"  Added: {png_path.name}")

            if not images:
                logger.error("No valid images found — keeping individual PNGs")
                return captured_files

            # First image is the base, rest are appended as additional pages
            images[0].save(
                merged_path,
                format='PDF',
                save_all=True,
                append_images=images[1:]
            )

            size_mb = merged_path.stat().st_size / (1024 * 1024)
            logger.info(f"PDF report created: {merged_path} ({size_mb:.2f} MB, {len(images)} page(s))")

            for png_path in captured_files:
                try:
                    png_path.unlink()
                except Exception as e:
                    logger.warning(f"  Could not delete {png_path.name}: {e}")

            return [merged_path]

        except Exception as e:
            logger.error(f"PDF conversion failed: {e} — keeping individual PNGs")
            return captured_files
    
    def _remote_copy(self, captured_files: List[Path]) -> None:
        """Copy captured files to REMOTE_COPY_PATH (optional, non-fatal).
        
        Transfer method is controlled by REMOTE_COPY_METHOD:
          - paramiko : use Python paramiko library 
          - local    : use shutil.copy2 for local/mounted paths
        
        Path formats:
          - Remote (paramiko):  user@host:/remote/dir
          - Local:              /some/mounted/path
        
        Errors are logged but do NOT cause the application to exit with failure.
        """
        remote_path = self.config.get('REMOTE_COPY_PATH')
        if not remote_path:
            logger.warning("SEND_IMG_TO_REMOTE is true but REMOTE_COPY_PATH is not set — skipping")
            return
        
        method = self.config.get('REMOTE_COPY_METHOD', 'paramiko').lower()
        
        logger.info("-" * 40)
        logger.info(f"Copying captures to: {remote_path} (method: {method})")
        logger.info("-" * 40)
        
        if method == 'paramiko':
            self._remote_copy_paramiko(captured_files, remote_path)
        else:  # local
            self._remote_copy_local(captured_files, remote_path)
    
    
    def _remote_copy_paramiko(self, captured_files: List[Path], remote_path: str) -> None:
        """Copy files using paramiko SFTP.

        Authentication is tried in order:
          1. SSH_KEY_PATH  — explicit key file from config
          2. SSH_PASSWORD  — password auth
          3. Auto-discover — /tmp/ssh_key (auto-mounted by run_report.sh), then
                             ~/.ssh/id_* paths (resolve to /tmp/.ssh/* when HOME=/tmp)
          4. SSH agent     — last resort
        """
        if not HAS_PARAMIKO:
            logger.error("paramiko is not installed — cannot use REMOTE_COPY_METHOD=paramiko")
            return

        # Parse user@host:/path
        if ':' not in remote_path or '@' not in remote_path.split(':')[0]:
            logger.error(f"Invalid remote path for paramiko: {remote_path} (expected user@host:/path)")
            return

        user_host, dest_dir = remote_path.split(':', 1)
        user, host = user_host.split('@', 1)
        ssh_port = self.config.get_int('SSH_PORT', 22)

        # Read policy once; used in _fresh_client() and save logic below.
        host_key_policy = self.config.get('SSH_HOST_KEY_POLICY', 'warn').lower()

        ssh_client = None
        sftp_client = None
        try:
            # ----- Resolve SSH directory -----
            home_dir = Path(os.path.expanduser('~'))
            ssh_dir = home_dir / '.ssh'
            try:
                ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                logger.debug(f"  SSH directory ready: {ssh_dir}")
            except OSError as e:
                logger.warning(f"  Cannot create SSH directory {ssh_dir}: {e}")

            # ----- Locate known_hosts -----
            known_hosts_candidates = [
                ssh_dir / 'known_hosts',               # /tmp/.ssh/known_hosts — mounted by run_report.sh
                Path('/home/appuser/.ssh/known_hosts'), # image default user (readable if same UID)
                Path('/tmp/.ssh/known_hosts'),          # explicit fallback
            ]

            seen: set = set()
            known_hosts_candidates = [
                p for p in known_hosts_candidates
                if not (str(p) in seen or seen.add(str(p)))
            ]

            known_hosts_path = None
            for candidate in known_hosts_candidates:
                if candidate.exists():
                    known_hosts_path = candidate
                    logger.debug(f"  Found known_hosts at {candidate}")
                    break

            if known_hosts_path is None:
                if host_key_policy == 'reject':
                    logger.error(
                        f"  SSH_HOST_KEY_POLICY=reject but no known_hosts file found.\n"
                        f"  Searched: {', '.join(str(c) for c in known_hosts_candidates)}\n"
                        f"  Mount known_hosts into the container:\n"
                        f"    -v ~/.ssh/known_hosts:/tmp/.ssh/known_hosts:ro\n"
                        f"  Or change SSH_HOST_KEY_POLICY to 'warn' or 'auto'."
                    )
                    return
                else:
                    # Guard against read-only bind-mounts.
                    candidate_path = ssh_dir / 'known_hosts'
                    try:
                        candidate_path.touch(mode=0o600)
                        known_hosts_path = candidate_path
                        logger.debug(f"  Created empty known_hosts at {known_hosts_path}")
                    except OSError as e:
                        logger.warning(
                            f"  Cannot create {candidate_path}: {e} — "
                            f"proceeding without a known_hosts file"
                        )
                        known_hosts_path = None

            def _fresh_client() -> paramiko.SSHClient:
                c = paramiko.SSHClient()
                if host_key_policy == 'reject':
                    c.set_missing_host_key_policy(paramiko.RejectPolicy())
                elif host_key_policy == 'auto':
                    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                else:
                    c.set_missing_host_key_policy(paramiko.WarningPolicy())
                    warnings.filterwarnings('ignore', category=UserWarning,
                                            module='paramiko')
                if known_hosts_path and known_hosts_path.exists():
                    try:
                        c.load_host_keys(str(known_hosts_path))
                        c._host_keys_filename = None
                    except Exception as e:
                        logger.warning(f"  Could not load known_hosts: {e}")
                try:
                    c.load_system_host_keys('/dev/null')
                except Exception:
                    pass
                return c

            # ----- Authentication -----
            ssh_key_path = self.config.get('SSH_KEY_PATH')
            ssh_password = self.config.get('SSH_PASSWORD')
            connected = False

            def _try_key_file(key_file: str) -> bool:
                """Decode a private key file and attempt key-based auth."""
                nonlocal ssh_client, connected
                kp = Path(key_file)
                if not kp.exists():
                    return False
                try:
                    logger.info(f"  Authenticating with SSH key: {key_file}")
                    pkey = None
                    for key_class in (paramiko.RSAKey, paramiko.Ed25519Key,
                                      paramiko.ECDSAKey, paramiko.DSSKey):
                        try:
                            pkey = key_class.from_private_key_file(str(kp))
                            break
                        except paramiko.ssh_exception.SSHException:
                            continue
                    if pkey is None:
                        logger.warning(
                            f"  Could not decode key file {key_file} "
                            f"(tried RSA, Ed25519, ECDSA, DSS) — skipping"
                        )
                        return False
                    ssh_client = _fresh_client()
                    ssh_client.connect(host, port=ssh_port, username=user,
                                       pkey=pkey, timeout=30,
                                       look_for_keys=False, allow_agent=False)
                    connected = True
                    logger.info("  SSH key authentication successful")
                    return True
                except (OSError, paramiko.ssh_exception.SSHException,
                        paramiko.AuthenticationException) as e:
                    logger.warning(f"  SSH key auth failed for {key_file}: {e}")
                    return False

            # Method 1: explicit SSH_KEY_PATH from config
            if ssh_key_path and not connected:
                _try_key_file(ssh_key_path)

            # Method 2: password
            if not connected and ssh_password:
                try:
                    logger.info("  Authenticating with SSH password")
                    ssh_client = _fresh_client()
                    ssh_client.connect(host, port=ssh_port, username=user,
                                       password=ssh_password, timeout=30,
                                       look_for_keys=False, allow_agent=False)
                    connected = True
                    logger.info("  SSH password authentication successful")
                except (paramiko.AuthenticationException,
                        paramiko.ssh_exception.SSHException) as e:
                    logger.warning(f"  SSH password auth failed: {e}")

            # Method 3: auto-discover key files.
            if not connected:
                discover_candidates = [
                    '/tmp/ssh_key',
                    str(home_dir / '.ssh' / 'id_ed25519'),
                    str(home_dir / '.ssh' / 'id_rsa'),
                    str(home_dir / '.ssh' / 'id_ecdsa'),
                    '/home/appuser/.ssh/id_ed25519',
                    '/home/appuser/.ssh/id_rsa',
                    '/home/appuser/.ssh/id_ecdsa',
                ]
                for key_candidate in discover_candidates:
                    if _try_key_file(key_candidate):
                        break

            # Method 4: SSH agent (last resort).
            if not connected:
                try:
                    logger.info("  Attempting SSH agent / default key discovery")
                    ssh_client = _fresh_client()
                    ssh_client.connect(host, port=ssh_port, username=user,
                                       timeout=30, allow_agent=True,
                                       look_for_keys=True)
                    connected = True
                    logger.info("  SSH agent/key discovery authentication successful")
                except (paramiko.AuthenticationException,
                        paramiko.ssh_exception.SSHException) as e:
                    logger.warning(f"  SSH agent auth failed: {e}")

            if not connected:
                logger.error(
                    "  No SSH authentication method succeeded. Configure one of:\n"
                    "    1. SSH_KEY_PATH  = path to private key inside the container\n"
                    "       run_report.sh auto-mounts the host key to /tmp/ssh_key\n"
                    "       automatically when REMOTE_COPY_METHOD=paramiko.\n"
                    "    2. SSH_PASSWORD  = password for the remote user\n"
                    "    3. Run ssh-copy-id so passwordless SSH works from the host\n"
                    "    4. Mount a key manually:\n"
                    "       docker run -v ~/.ssh/id_rsa:/tmp/ssh_key:ro ..."
                )
                return

            # ----- Save learned host keys (best-effort, skip if read-only) -----
            if host_key_policy in ('warn', 'auto') and known_hosts_path:
                try:
                    save_path = known_hosts_path
                    try:
                        save_path.touch(exist_ok=True)
                    except OSError:
                        save_path = ssh_dir / 'known_hosts'
                    ssh_client.save_host_keys(str(save_path))
                    logger.debug(f"  Saved host keys to {save_path}")
                except Exception as e:
                    logger.debug(f"  Could not save host keys: {e}")

            sftp_client = ssh_client.open_sftp()

            # Ensure remote directory exists
            try:
                sftp_client.stat(dest_dir)
            except FileNotFoundError:
                # Create remote directory (mkdir -p equivalent)
                parts = dest_dir.split('/')
                current = ''
                for part in parts:
                    if not part:
                        current = '/'
                        continue
                    current = current + part + '/'
                    try:
                        sftp_client.stat(current)
                    except FileNotFoundError:
                        sftp_client.mkdir(current)

            for file_path in captured_files:
                try:
                    remote_file = f"{dest_dir.rstrip('/')}/{file_path.name}"
                    sftp_client.put(str(file_path), remote_file)
                    logger.info(f"  Copied (paramiko): {file_path.name}")
                except Exception as e:
                    logger.error(f"  Paramiko SFTP failed for {file_path.name}: {e}")

        except paramiko.AuthenticationException:
            logger.error(
                f"  Paramiko authentication failed for {user}@{host}.\n"
                f"  Ensure SSH_KEY_PATH points to a valid private key, or set SSH_PASSWORD.\n"
                f"  run_report.sh auto-mounts the host key to /tmp/ssh_key when\n"
                f"  REMOTE_COPY_METHOD=paramiko. Or set SSH_PASSWORD in .env."
            )
        except paramiko.SSHException as e:
            logger.error(f"  Paramiko SSH error: {e}")
        except Exception as e:
            logger.error(f"  Paramiko connection error: {e}")
        finally:
            if sftp_client:
                sftp_client.close()
            if ssh_client:
                ssh_client.close()

    def _remote_copy_local(self, captured_files: List[Path], remote_path: str) -> None:
        """Copy files to a local/mounted path."""
        dest_dir = Path(remote_path)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"  Cannot create local directory {remote_path}: {e}")
            return
        
        for file_path in captured_files:
            try:
                dest_file = dest_dir / file_path.name
                shutil.copy2(str(file_path), str(dest_file))
                logger.info(f"  Copied (local): {file_path.name} → {dest_file}")
            except Exception as e:
                logger.error(f"  Local copy failed for {file_path.name}: {e}")


def signal_handler(signum, frame):
    """Handle interrupt signals gracefully."""
    logger.warning(f"Received signal {signum}, shutting down...")
    sys.exit(128 + signum)


def main():
    """Entry point for the application."""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    env_file = os.environ.get('ENV_FILE', None)
    if not env_file:
        for path in ['.env', '/app/.env', '/app/config/.env']:
            if Path(path).exists():
                env_file = path
                break
    
    app = Application(env_file)
    exit_code = app.run()
    
    sys.exit(exit_code)


if __name__ == '__main__':
    main()