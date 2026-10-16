import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import PLUGIN_ID


LOG_TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'
WORKER_SINGLE_INSTANCE_EXIT_CODE = 3
LOG_LINE_PATTERN = re.compile(
    r'^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) '
    r'(?P<level>[A-Z]+) (?P<message>.*)$'
)
DEFAULT_CONFIG: dict[str, object] = {
    'username': '',
    'password': '',
    'domain': '',
    'portal_origin': 'https://net.szu.edu.cn',
    'ac_id': '12',
    'device': 'Windows 10',
    'platform': 'Windows',
    'connectivity_test_url': 'https://www.baidu.com/',
    'failure_threshold': 3,
    'connectivity_timeout_seconds': 8.0,
    'probe_interval_seconds': 300.0,
    'login_verify_attempts': 5,
    'login_verify_interval_seconds': 1.0,
    'login_retry_cooldown_seconds': 30.0,
    'request_timeout_seconds': 10.0,
    'captcha_id': '',
    'captcha_value': '',
}


class CampusStatusItem(BaseModel):
    label: str
    status: str
    detail: str
    tone: str
    checked_at: str | None = None


class CampusHistoryPoint(BaseModel):
    start_at: str
    end_at: str
    checked_at: str | None = None
    status: str
    detail: str
    tone: str


class CampusHistoryItem(BaseModel):
    label: str
    status: str
    detail: str
    checks: int
    successful_checks: int
    failed_checks: int
    success_rate: float | None = None
    latest_at: str | None = None
    points: list[CampusHistoryPoint] = Field(default_factory=list)


class CampusNetworkStatus(BaseModel):
    current: CampusStatusItem
    listener: CampusStatusItem
    history: list[CampusHistoryItem]
    username: str
    password_configured: bool
    credentials_configured: bool
    probe_interval_seconds: int
    updated_at: str


@dataclass(frozen=True)
class LogRecord:
    timestamp: datetime
    level: str
    message: str


class ColszunetPlugin:
    plugin_id = PLUGIN_ID

    def __init__(
        self,
        worker_root: Path,
        config_store: object,
        process_registry: Any,
    ) -> None:
        self.root = worker_root
        self.script_path = worker_root / 'colszunet.py'
        self.config_store = config_store
        self.process_registry = process_registry
        self.legacy_config_path = worker_root / 'config.json'
        self.config_path = config_store.path
        self._migrate_legacy_config()
        self.log_path = worker_root / 'colszunet.log'
        self.process: subprocess.Popen[bytes] | None = None
        self.lock = threading.Lock()

    def start(self) -> None:
        with self.lock:
            self._start_locked()

    def stop(self) -> None:
        with self.lock:
            self._stop_locked()

    def runtime_status(self) -> dict[str, object]:
        with self.lock:
            process = self.process
            if process is None:
                return {
                    'status': 'stopped',
                    'detail': '后台监控程序未运行',
                    'exit_code': None,
                }
            exit_code = process.poll()

        if exit_code is None:
            return {
                'status': 'running',
                'detail': 'colszunet 正在后台运行',
                'exit_code': None,
            }
        if exit_code == WORKER_SINGLE_INSTANCE_EXIT_CODE:
            return {
                'status': 'running',
                'detail': 'colszunet 已由其他进程运行，当前启动请求已经退出',
                'exit_code': exit_code,
            }
        if exit_code == 0:
            return {
                'status': 'stopped',
                'detail': '后台监控程序已经停止',
                'exit_code': exit_code,
            }
        return {
            'status': 'error',
            'detail': f'后台监控程序异常退出，退出代码为 {exit_code}',
            'exit_code': exit_code,
        }

    def update_settings(self, settings: dict[str, object], enabled: bool) -> None:
        supported_keys = {
            'username',
            'password',
            'probe_interval_seconds',
            'clear_credentials',
        }
        unknown_keys = sorted(set(settings) - supported_keys)
        if unknown_keys:
            raise ValueError(f'unsupported settings: {", ".join(unknown_keys)}')
        if not settings:
            raise ValueError('settings must not be empty')

        with self.lock:
            config = self._read_config_locked()
            updated_config = dict(config)
            clear_credentials = settings.get('clear_credentials', False)
            if not isinstance(clear_credentials, bool):
                raise ValueError('clear_credentials must be a boolean')
            if clear_credentials:
                updated_config['username'] = ''
                updated_config['password'] = ''
            else:
                if 'username' in settings:
                    username = settings['username']
                    if not isinstance(username, str):
                        raise ValueError('username must be a string')
                    updated_config['username'] = username
                if 'password' in settings:
                    password = settings['password']
                    if not isinstance(password, str):
                        raise ValueError('password must be a string')
                    if password or not self._credentials_configured(config):
                        updated_config['password'] = password
            if 'probe_interval_seconds' in settings:
                value = settings['probe_interval_seconds']
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 1:
                    raise ValueError('probe_interval_seconds must be a positive number')
                if isinstance(value, float) and not value.is_integer():
                    raise ValueError('probe_interval_seconds must be an integer')
                updated_config['probe_interval_seconds'] = int(value)
            if updated_config == config:
                return
            self._write_config_locked(updated_config)
            self._stop_locked()
            if enabled and self._credentials_configured(updated_config):
                self._start_locked()

    def status(self, enabled: bool) -> CampusNetworkStatus:
        records = self._read_records()
        current = self._current_status(records)
        with self.lock:
            config = self._read_config_locked()
            username = self._config_text(config, 'username')
            password = self._config_text(config, 'password')
            credentials_configured = self._credentials_configured(config)
            probe_interval_seconds = self._probe_interval_seconds_locked()
        listener = self._listener_status(enabled, credentials_configured)
        now = datetime.now()
        history = [
            self._history_status('24h', now - timedelta(hours=24), records, now, 48),
            self._history_status('7d', now - timedelta(days=7), records, now, 168),
        ]
        return CampusNetworkStatus(
            current=current,
            listener=listener,
            history=history,
            username=username,
            password_configured=bool(password),
            credentials_configured=credentials_configured,
            probe_interval_seconds=probe_interval_seconds,
            updated_at=now.strftime(LOG_TIMESTAMP_FORMAT),
        )

    def _start_locked(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                return
            self.process.wait()
            self.process_registry.unregister(self.process)
            self.process = None
        if not self.script_path.is_file():
            raise FileNotFoundError(f'colszunet script does not exist: {self.script_path}')
        config = self._read_config_locked()
        if not self._credentials_configured(config):
            self.process = None
            return
        if not self.config_path.is_file():
            raise FileNotFoundError(f'colpilot config does not exist: {self.config_path}')
        process = subprocess.Popen(
            [
                sys.executable,
                str(self.script_path),
                '--config',
                str(self.config_path),
                '--section',
                self.plugin_id,
            ],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.process = self.process_registry.register(f'{self.plugin_id}:worker', process)

    def _stop_locked(self) -> None:
        if self.process is None:
            return
        self.process_registry.stop_process(self.process)
        self.process = None

    def _read_config_locked(self) -> dict[str, object]:
        raw_settings = self.config_store.read_section(self.plugin_id)
        unknown_keys = sorted(set(raw_settings) - set(DEFAULT_CONFIG))
        if unknown_keys:
            raise ValueError(f'unsupported saved settings: {", ".join(unknown_keys)}')

        settings = dict(DEFAULT_CONFIG)
        for key, raw_value in raw_settings.items():
            default_value = DEFAULT_CONFIG[key]
            if isinstance(default_value, str):
                settings[key] = raw_value
            elif isinstance(default_value, int):
                settings[key] = self._parse_integer(raw_value, key)
            elif isinstance(default_value, float):
                settings[key] = self._parse_number(raw_value, key)
            else:
                raise ValueError(f'配置项类型不受支持：{key}')
        return settings

    def _write_config_locked(self, config: dict[str, object]) -> None:
        self.config_store.update_section(
            self.plugin_id,
            config,
        )

    def _migrate_legacy_config(self) -> None:
        self.config_store.migrate_json_section(
            self.plugin_id,
            self.legacy_config_path,
            set(DEFAULT_CONFIG),
            self._should_migrate_legacy_config,
        )

    @staticmethod
    def _should_migrate_legacy_config(config: dict[str, object]) -> bool:
        for key, value in config.items():
            if key in DEFAULT_CONFIG and value != DEFAULT_CONFIG[key]:
                return True
        return False

    @staticmethod
    def _parse_integer(value: str, key: str) -> int:
        try:
            return int(value.strip())
        except ValueError as error:
            raise ValueError(f'配置项 {key} 需要使用整数') from error

    @staticmethod
    def _parse_number(value: str, key: str) -> float:
        try:
            return float(value.strip())
        except ValueError as error:
            raise ValueError(f'配置项 {key} 需要使用数字') from error

    @staticmethod
    def _config_text(config: dict[str, object], key: str) -> str:
        value = config.get(key)
        if not isinstance(value, str):
            raise ValueError(f'colszunet config field must be a string: {key}')
        return value

    @classmethod
    def _credentials_configured(cls, config: dict[str, object]) -> bool:
        return bool(cls._config_text(config, 'username')) and bool(
            cls._config_text(config, 'password')
        )

    def _probe_interval_seconds_locked(self) -> int:
        value = self._read_config_locked().get('probe_interval_seconds')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 1:
            raise ValueError('probe_interval_seconds must be a positive number')
        if isinstance(value, float) and not value.is_integer():
            raise ValueError('probe_interval_seconds must be an integer')
        return int(value)

    def _read_records(self) -> list[LogRecord]:
        if not self.log_path.is_file():
            return []
        records: list[LogRecord] = []
        for line in self.log_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            match = LOG_LINE_PATTERN.match(line)
            if match is None:
                continue
            records.append(
                LogRecord(
                    timestamp=datetime.strptime(
                        match.group('timestamp'),
                        LOG_TIMESTAMP_FORMAT,
                    ),
                    level=match.group('level'),
                    message=match.group('message'),
                )
            )
        return records

    def _current_status(self, records: list[LogRecord]) -> CampusStatusItem:
        connectivity_records = [
            record for record in records if self._is_connectivity_record(record)
        ]
        if not connectivity_records:
            return CampusStatusItem(
                label='校园网目前状态',
                status='等待检测',
                detail='等待 colszunet 写入首次连通性结果',
                tone='pending',
            )

        latest = connectivity_records[-1]
        if latest.message == '连通性测试成功':
            return CampusStatusItem(
                label='校园网目前状态',
                status='在线',
                detail='外部网络访问正常',
                tone='online',
                checked_at=latest.timestamp.strftime(LOG_TIMESTAMP_FORMAT),
            )
        return CampusStatusItem(
            label='校园网目前状态',
            status='离线',
            detail=latest.message.removeprefix('连通性测试失败：'),
            tone='offline',
            checked_at=latest.timestamp.strftime(LOG_TIMESTAMP_FORMAT),
        )

    def _listener_status(
        self,
        enabled: bool,
        credentials_configured: bool,
    ) -> CampusStatusItem:
        if not enabled:
            return CampusStatusItem(
                label='监听状态',
                status='已停用',
                detail='插件已停用，后台监控进程未运行',
                tone='offline',
            )
        if not credentials_configured:
            return CampusStatusItem(
                label='监听状态',
                status='待配置',
                detail='请填写校园网账号和密码后启动监控',
                tone='pending',
            )
        runtime = self.runtime_status()
        if runtime['status'] == 'running':
            return CampusStatusItem(
                label='监听状态',
                status='监听中',
                detail='colszunet 正在持续检查网络连通性',
                tone='online',
            )
        if runtime['status'] == 'error':
            return CampusStatusItem(
                label='监听状态',
                status='运行错误',
                detail=str(runtime['detail']),
                tone='offline',
            )
        return CampusStatusItem(
            label='监听状态',
            status='已停止',
            detail=str(runtime['detail']),
            tone='offline',
        )

    def _history_status(
        self,
        label: str,
        since: datetime,
        records: list[LogRecord],
        until: datetime,
        bucket_count: int,
    ) -> CampusHistoryItem:
        connectivity_records = [
            record
            for record in records
            if record.timestamp >= since and self._is_connectivity_record(record)
        ]
        successful_checks = sum(
            record.message == '连通性测试成功' for record in connectivity_records
        )
        failed_checks = len(connectivity_records) - successful_checks
        checks = len(connectivity_records)
        latest_at = (
            connectivity_records[-1].timestamp.strftime(LOG_TIMESTAMP_FORMAT)
            if connectivity_records
            else None
        )
        points = self._history_points(since, until, connectivity_records, bucket_count)
        if checks == 0:
            return CampusHistoryItem(
                label=label,
                status='暂无数据',
                detail='当前时间范围内没有连通性记录',
                checks=0,
                successful_checks=0,
                failed_checks=0,
                latest_at=latest_at,
                points=points,
            )

        success_rate = round(successful_checks / checks * 100, 1)
        status = '正常' if failed_checks == 0 else '有波动'
        detail = f'检测 {checks} 次，成功 {successful_checks} 次，失败 {failed_checks} 次'
        return CampusHistoryItem(
            label=label,
            status=status,
            detail=detail,
            checks=checks,
            successful_checks=successful_checks,
            failed_checks=failed_checks,
            success_rate=success_rate,
            latest_at=latest_at,
            points=points,
        )

    def _history_points(
        self,
        since: datetime,
        until: datetime,
        records: list[LogRecord],
        bucket_count: int,
    ) -> list[CampusHistoryPoint]:
        bucket_duration = (until - since) / bucket_count
        points: list[CampusHistoryPoint] = []
        for index in range(bucket_count):
            bucket_start = since + bucket_duration * index
            bucket_end = until if index == bucket_count - 1 else since + bucket_duration * (index + 1)
            bucket_records = [
                record
                for record in records
                if bucket_start <= record.timestamp < bucket_end
                or index == bucket_count - 1 and record.timestamp == bucket_end
            ]
            failed_records = [
                record for record in bucket_records if record.message != '连通性测试成功'
            ]
            latest_at = (
                bucket_records[-1].timestamp.strftime(LOG_TIMESTAMP_FORMAT)
                if bucket_records
                else None
            )
            if not bucket_records:
                points.append(
                    CampusHistoryPoint(
                        start_at=bucket_start.strftime(LOG_TIMESTAMP_FORMAT),
                        end_at=bucket_end.strftime(LOG_TIMESTAMP_FORMAT),
                        status='暂无数据',
                        detail='该时间段没有连通性记录',
                        tone='pending',
                    )
                )
                continue
            if failed_records:
                points.append(
                    CampusHistoryPoint(
                        start_at=bucket_start.strftime(LOG_TIMESTAMP_FORMAT),
                        end_at=bucket_end.strftime(LOG_TIMESTAMP_FORMAT),
                        checked_at=latest_at,
                        status='失败',
                        detail=f'检测 {len(bucket_records)} 次，失败 {len(failed_records)} 次',
                        tone='offline',
                    )
                )
                continue
            points.append(
                CampusHistoryPoint(
                    start_at=bucket_start.strftime(LOG_TIMESTAMP_FORMAT),
                    end_at=bucket_end.strftime(LOG_TIMESTAMP_FORMAT),
                    checked_at=latest_at,
                    status='成功',
                    detail=f'检测 {len(bucket_records)} 次，全部成功',
                    tone='online',
                )
            )
        return points

    @staticmethod
    def _is_connectivity_record(record: LogRecord) -> bool:
        return record.message == '连通性测试成功' or record.message.startswith('连通性测试失败：')


def create_plugin(
    plugin_root: Path,
    config_store: object,
    process_registry: object,
) -> ColszunetPlugin:
    return ColszunetPlugin(plugin_root / 'worker', config_store, process_registry)
