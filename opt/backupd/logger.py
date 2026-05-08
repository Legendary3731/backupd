import logging
import os
import time

LOG_DIR = "/var/log/backupd"
LOG_RETENTION_DAYS = 14

GREY   = "\033[38;5;245m"
GREEN  = "\033[38;5;82m"
YELLOW = "\033[38;5;220m"
RED    = "\033[38;5;196m"
BLUE   = "\033[38;5;75m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

LEVEL_STYLES = {
    "DEBUG":    f"{GREY}DEBUG  {RESET}",
    "INFO":     f"{GREEN}INFO   {RESET}",
    "WARNING":  f"{YELLOW}WARN   {RESET}",
    "ERROR":    f"{RED}{BOLD}ERROR  {RESET}",
    "CRITICAL": f"{RED}{BOLD}CRIT   {RESET}",
}


class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        ts      = self.formatTime(record, "%H:%M:%S")
        level   = LEVEL_STYLES.get(record.levelname, record.levelname)
        name    = f"{BLUE}{record.name:<10}{RESET}"
        message = record.getMessage()

        line = f"{GREY}{ts}{RESET}  {level}  {name}  {message}"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


class FileFormatter(logging.Formatter):
    def format(self, record):
        ts      = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        level   = f"{record.levelname:<8}"
        message = record.getMessage()

        line = f"{ts} | {level} | {record.name} | {message}"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


def purge_old_logs():
    if not os.path.exists(LOG_DIR):
        return
    now = time.time()
    for f in os.scandir(LOG_DIR):
        if f.name.endswith(".log") and now - f.stat().st_mtime > LOG_RETENTION_DAYS * 86400:
            os.unlink(f.path)


def get_logger(name: str) -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    purge_old_logs()

    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Fichier — tout à partir de INFO
    file_handler = logging.FileHandler(f"{LOG_DIR}/{name}.log")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(FileFormatter())

    # Console — tout à partir de INFO
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ConsoleFormatter())

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger