'''Run CoinCourier's one-time Google Drive OAuth authorization on a local computer.'''

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from google_auth_oauthlib.flow import InstalledAppFlow


SCOPE = 'https://www.googleapis.com/auth/drive.file'


def read_installed_client(path: Path) -> dict[str, Any]:
    '''Validate a Desktop OAuth client JSON downloaded from Google Cloud.'''
    if not path.is_file():
        raise FileNotFoundError(f'Google OAuth Desktop client file does not exist: {path}')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not isinstance(payload.get('installed'), dict):
        raise ValueError('Expected OAuth client JSON for Application type: Desktop app')
    installed: dict[str, Any] = payload['installed']
    if not isinstance(installed.get('client_id'), str) or not installed['client_id']:
        raise ValueError('OAuth client JSON is missing installed.client_id')
    if not isinstance(installed.get('client_secret'), str) or not installed['client_secret']:
        raise ValueError('OAuth client JSON is missing installed.client_secret')
    return installed


def write_secret_env(path: Path, values: dict[str, str]) -> None:
    '''Create the local ignored secrets file; never silently overwrite it.'''
    if any('\n' in value or '\r' in value or not value for value in values.values()):
        raise ValueError('OAuth response contains an invalid credential value')
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        for key, value in values.items():
            handle.write(f'{key}={value}\n')


def main() -> int:
    '''Authorize locally using the browser, saving Dokploy values without printing tokens.'''
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client-secrets', required=True, type=Path,
                        help='Absolute path to Google OAuth Desktop client JSON')
    parser.add_argument('--output', type=Path, default=Path('.secrets/google-drive.env'),
                        help='Private local env file created only once')
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise FileExistsError(f'Refusing to replace existing credentials: {args.output}')
        client = read_installed_client(args.client_secrets)
        flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secrets), [SCOPE])
        credentials = flow.run_local_server(port=0, open_browser=True,
                                            prompt='consent', access_type='offline')
        if not credentials.refresh_token:
            raise RuntimeError('Google did not return a refresh token; verify consent settings')
        write_secret_env(args.output, {
            'GOOGLE_OAUTH_CLIENT_ID': client['client_id'],
            'GOOGLE_OAUTH_CLIENT_SECRET': client['client_secret'],
            'GOOGLE_OAUTH_REFRESH_TOKEN': credentials.refresh_token,
        })
    except (FileExistsError, FileNotFoundError, ValueError, RuntimeError, OSError) as error:
        print(f'Authorization setup failed: {error}', file=sys.stderr)
        return 1
    print(f'OAuth completed. Credentials saved locally at: {args.output.resolve()}')
    print('Keep the file private. Paste its three values into Dokploy protected environment settings.')
    print('Do not commit, upload to chat, or include it in a Docker image.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
