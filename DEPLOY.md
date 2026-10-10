# Deploying GICC on Render

## Start command

Package build:      uvicorn app.main:app --host 0.0.0.0 --port $PORT
Single-file build:  uvicorn gicc_single_file:app --host 0.0.0.0 --port $PORT

Build command:      pip install -r requirements.txt

## Repository layout

requirements.txt, render.yaml and app/ must sit at the **root** of the repo,
not inside a subfolder. If the build fails with

    ERROR: Could not open requirements file:
    [Errno 2] No such file or directory: 'requirements.txt'

then the files are one level down. Either move them up, or set
Settings -> Build & Deploy -> Root Directory to the folder that contains them.

## Required settings

DATA_DIR=/var/data with a persistent disk mounted at that path. Without it the
database and every uploaded document are destroyed on each deploy, silently -
the app starts fine and comes back empty.

SECRET_KEY - any long random string.
ADMIN_PASSWORD - set before the first deploy.

## Python version

Pinned to 3.12.3 in two places:

  .python-version   read by Render regardless of how the service was created
  render.yaml       PYTHON_VERSION, applies only to blueprint-created services

If a service is created by hand in the dashboard, render.yaml is not read at
all, so the env vars in it never apply. That is how a build can install Python
3.14 wheels while render.yaml says 3.12.3.

## If the build succeeds but the server exits immediately

Check which Python and which starlette the build log installed. FastAPI pins
starlette only as >=0.46.0, so an unpinned build takes whatever is newest that
day. Both are now pinned in requirements.txt.

Look above the "Exited with status 1" line in the Render log - the Python
traceback is printed there and names the actual failure.

## Operator sign-in

The operator console is at **/admin/login**, not /login.

They are separate tables by design: a platform operator is never "a user with a
flag", so a stolen supplier session cannot escalate. The consequence is that
admin credentials entered at /login always fail with "did not match" - which
looks exactly like a wrong password.

Defaults (overridden by ADMIN_EMAIL / ADMIN_PASSWORD):

    admin@gicc.platform / admin1234

## Locked out of the operator console

The operator account is seeded once, when the table is empty. Changing
ADMIN_PASSWORD later has no effect on a database that already has one.

To reset:

  1. Set ADMIN_RESET=1 and ADMIN_PASSWORD=<new password>
  2. Redeploy. The log prints:
       [admin] ADMIN_RESET applied: <email> password reset.
  3. Sign in, then unset ADMIN_RESET and redeploy again.

Leaving ADMIN_RESET set means every deploy rewrites the operator password from
the environment.

## Cloud folders (Google Drive, OneDrive)

The demo folder works with no setup. Real providers need an OAuth app each.

Set PUBLIC_BASE_URL to the public https address of the service, e.g.
https://tsp.onrender.com. Providers reject a redirect URI that differs even by
scheme, and behind Render's proxy the app can otherwise see itself as http.

### Google Drive

1. Google Cloud console -> APIs & Services -> enable the Google Drive API.
2. OAuth consent screen: External, add the scope .../auth/drive.readonly.
3. Credentials -> OAuth client ID -> Web application.
   Authorised redirect URI: <PUBLIC_BASE_URL>/connections/google/callback
4. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.

drive.readonly is a *restricted* scope. While the app is in Testing mode only
the test users you list can connect (up to 100). Publishing it for everyone
requires Google's verification, including a third-party security assessment.
Plan for weeks, not days. The alternative is drive.file with the Google Picker,
which avoids the assessment but only sees files the user picks one at a time.

### Microsoft OneDrive

1. Entra admin centre -> App registrations -> New registration.
   Supported accounts: any organisational directory and personal accounts.
   Redirect URI (Web): <PUBLIC_BASE_URL>/connections/microsoft/callback
2. Certificates & secrets -> new client secret.
3. API permissions -> Microsoft Graph, delegated: Files.Read.All, User.Read,
   offline_access.
4. Set MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET (and MICROSOFT_TENANT if
   you want to restrict to one directory; default "common").

Many organisations block users from consenting to Files.Read.All themselves.
Their IT admin grants consent once for the organisation, after which every user
can connect.

### Schedule

A background loop checks every CLOUD_SYNC_LOOP_MINUTES (default 5) for folders
that are due. It runs inside the web process, so on a plan that sleeps when idle
scheduled checks only happen while the service is awake. Keep the instance on an
always-on plan if scheduled checks matter.

Tokens are encrypted with a key derived from SECRET_KEY. Changing SECRET_KEY
makes stored tokens unreadable; affected folders show "Reconnect needed".
