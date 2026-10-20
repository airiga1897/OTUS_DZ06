"""One credential schema; environment by default, explicit private file for development."""
import json
import os
from pathlib import Path
import subprocess
from typing import Any


def auth_settings() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / 'config/selectel-auth.json'
    return json.loads(path.read_text(encoding='utf-8-sig'))


def assert_private_file(path: Path) -> None:
    if os.name != 'nt':
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ValueError('Development credentials must be accessible only to their owner')
        return
    command = "$a=Get-Acl -LiteralPath $env:OTUS_PRIVATE_FILE_CHECK -ErrorAction Stop; " \
              "$s=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; " \
              "if($a.Access | Where-Object {$_.AccessControlType -eq 'Allow' -and " \
              "$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -ne $s}){exit 1}"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', command],
                            env={**os.environ, 'OTUS_PRIVATE_FILE_CHECK': str(path.resolve())}, capture_output=True)
    if result.returncode:
        raise ValueError('Development credentials must be accessible only to the current user')


def read_private_file(path: Path) -> dict[str, str]:
    assert_private_file(path)
    return json.loads(path.read_text(encoding='utf-8-sig'))


def config_from_env() -> dict[str, str]:
    settings = auth_settings()
    return {**{k: settings[k] for k in ['auth_url', 'region']},
            **{k: os.environ.get(v['env'], '') for k, v in settings['fields'].items()}}


def load_credentials() -> dict[str, str]:
    settings = auth_settings()
    selected = os.environ.get('OTUS_SELECTEL_AUTH')
    supplied = read_private_file(Path(selected)) if selected else config_from_env()
    config = {**{k: settings[k] for k in ['auth_url', 'region']}, **supplied}
    if any(not isinstance(config.get(k), str) or not config[k].strip() for k in settings['fields']):
        raise ValueError('Incomplete Selectel credentials; run scripts/env.ps1')
    if config['auth_url'] != settings['auth_url'] or config['region'] != settings['region']:
        raise ValueError('Unsupported Selectel endpoint or region')
    return config


def to_environment(config: dict[str, str]) -> dict[str, str]:
    settings = auth_settings()
    return {**{v['env']: config[k] for k, v in settings['fields'].items()},
            'OS_AUTH_URL': settings['auth_url'], 'OS_REGION_NAME': settings['region']}


def credential_values(config: dict[str, str] | None) -> list[str]:
    return [v for k in auth_settings()['fields'] if (v := (config or {}).get(k))]
