"""
One-time script to get Google OAuth2 refresh token.

Setup:
1. Go to Google Cloud Console: https://console.cloud.google.com/
2. Create or select a project
3. Enable Google Drive API
4. Go to "Credentials" -> "Create Credentials" -> "OAuth client ID"
5. Select "Desktop app" as application type
6. Download the credentials and copy client_id and client_secret

Run this script:
    python get_google_token.py

Then copy the refresh_token to your .env file.
"""

import os
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main():
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first")
        return

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    credentials = flow.run_local_server(port=8080)

    print("\n" + "=" * 50)
    print("Success! Copy this refresh token to your .env file:")
    print("=" * 50)
    print(f"\nGOOGLE_REFRESH_TOKEN={credentials.refresh_token}\n")


if __name__ == "__main__":
    main()
