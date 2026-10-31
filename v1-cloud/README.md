# Hacktober-Fest_X_ray
This an agent where you upload an X-ray image and it reads and analyze and tells you what's wrong
## Run the connected frontend
Install Python, then from this repository run:

```powershell
python -m pip install -r X_ray_Examiner/requirements.txt
$env:XRAY_BACKEND_URL = 'https://YOUR-BACKEND-HOST'
python X_ray_Examiner/app.py
```

Open http://127.0.0.1:5000. Use the backend server base URL, without `/api`.
The frontend gets body parts and imaging types from `/api/options` and sends
uploads and questions to `/api/analyze?wait=true`. Gemma runs on the backend;
local Ollama is no longer needed by the frontend. The default backend address is the team Codespaces URL configured in services/backend_client.py. A disconnected backend displays a connection error.


## Local MedPeek accounts
Open /register to create an account; /login signs in. Usernames are case-insensitive.
Passwords require 12–128 characters and are stored as Werkzeug scrypt hashes.
Accounts and revocable 12-hour sessions live in X_ray_Examiner/instance/accounts.sqlite3.
The signing key is persisted in instance/session.key. The entire instance folder is ignored by Git.
Keep this folder private and backed up; do not sync it through GitHub Desktop.
There is no password recovery or email verification yet. This login protects the frontend,
not direct access to the separately hosted backend API. For HTTPS deployment set MEDPEEK_HTTPS=1.
New chat keeps the account signed in; sign out revokes the current login session.

## Independent backend checker
Run `python X_ray_Examiner/diagnostics.py` and open http://127.0.0.1:5001.
This loopback-only tool is separate from login and chat. It checks API/Ollama/model availability,
and can submit one synthetic gray image to verify real inference with queue status.
It shares XRAY_BACKEND_URL with the frontend. Health checks do not prove inference works.

## Saved conversations
Conversations, message text, attachment filenames and completed analysis results are saved per account in the ignored local instance/accounts.sqlite3 database. Uploaded image bytes are not retained. Pending jobs are collected while the frontend server is running and resumed after restart if the remote backend still has them. Earlier unsaved browser conversations cannot be recovered automatically.
