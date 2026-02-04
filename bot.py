import os, re, sqlite3, logging, textwrap, threading, requests

from dotenv import load_dotenv

from slack_bolt import App

from slack_bolt.adapter.socket_mode import SocketModeHandler

import spotipy

from spotipy.oauth2 import SpotifyOAuth

 

# ----- Setup -----

logging.basicConfig(level=logging.INFO)

load_dotenv()  # loads .env

 

SLACK_CHANNEL_ID = os.environ["SLACK_CHANNEL_ID"]

PLAYLIST_ID = os.environ["SPOTIFY_PLAYLIST_ID"]

DEDUP_DB_PATH = os.environ.get("DEDUP_DB", "seen_tracks.sqlite3")

 

# ----- HTTP session (override cert verification) -----

session = requests.Session()

session.verify = True

 

# Regex for Spotify TRACK links/URIs:

# matches open.spotify.com/track/<22 chars> OR spotify:track:<22 chars>

TRACK_RE = re.compile(r"(?:open\.spotify\.com/track/|spotify:track:)([A-Za-z0-9]{22})")

 

# ----- Spotify client -----

CA_BUNDLE = r"C:\Users\[insert your user name]\slack-spotify-bot\corp_certifi.pem"

 

session = requests.Session()

session.verify = CA_BUNDLE

sp = spotipy.Spotify(

    auth_manager=SpotifyOAuth(

        client_id=os.environ["SPOTIFY_CLIENT_ID"],

        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],

        redirect_uri=os.environ["SPOTIFY_REDIRECT_URI"],

        scope="playlist-modify-public playlist-modify-private",

        open_browser=True,

        requests_session=session,

    )

)

 

# ----- Dedupe DB (avoid adding same song twice) -----

db = sqlite3.connect(DEDUP_DB_PATH, check_same_thread=False)

db.execute("CREATE TABLE IF NOT EXISTS seen (track_id TEXT PRIMARY KEY)")

db.commit()

db_lock = threading.Lock()

 

def mark_if_new(track_id: str) -> bool:

    with db_lock:

        try:

            # INSERT OR IGNORE avoids raising on duplicates

            db.execute("INSERT INTO seen(track_id) VALUES (?)", (track_id,))

            db.commit()

            # rowcount == 1 means it was newly inserted

            return db.total_changes > 0

        except sqlite3.IntegrityError:

            return False

 

# ----- Slack app -----

app = App(token=os.environ["SLACK_BOT_TOKEN"])

 

def extract_track_ids(text: str) -> set[str]:

    if not text:

        return set()

    return set(TRACK_RE.findall(text))

 

@app.event("message")

def on_message(body, logger):

    event = body.get("event", {}) or {}

 

    # Only process messages in our chosen channel; ignore bot messages to prevent loops

    if event.get("channel") != SLACK_CHANNEL_ID or event.get("subtype") == "bot_message":

        return

 

    text = event.get("text") or ""

 

    # Sometimes Slack unfurls add links in attachments; check a couple common fields

    attachments = event.get("attachments") or []

    att_urls = " ".join(a.get("title_link") or a.get("from_url") or "" for a in attachments)

 

    ids = extract_track_ids(text + " " + att_urls)

    if not ids:

        return

 

    new_ids = [tid for tid in ids if mark_if_new(tid)]

    if not new_ids:

        logger.info("No new tracks to add (duplicates).")

        return

 

    # Add tracks to the playlist (Spotify accepts up to 100 per call)

    uris = [f"spotify:track:{tid}" for tid in new_ids]

    sp.playlist_add_items(PLAYLIST_ID, uris)

    logger.info(f"Added {len(new_ids)} track(s) to playlist.")

 

    # Friendly Slack confirmation (reply in thread)

    try:

        tracks = sp.tracks(new_ids)["tracks"]

        lines = []

        for t in tracks:

            if not t:

                continue

            name = t.get("name") or "Unknown"

            artists = ", ".join(a.get("name") for a in t.get("artists", []) if a)

            lines.append(f"• {name} — {artists}" if artists else f"• {name}")

 

        preview = "\n".join(lines[:5])

        overflow = len(lines) - 5

        if overflow > 0:

            preview += f"\n…and {overflow} more"

 

        msg = textwrap.dedent(f"""\

            ✅ Added {len(new_ids)} track(s) to the [insert playlist title] Spotify playlist!

            {preview}

            """

).strip()

 

        app.client.chat_postMessage(

            channel=SLACK_CHANNEL_ID,

            thread_ts=event.get("ts"),

            text=msg,

        )

    except Exception as e:

        logger.warning(f"Tracks added but reply failed: {e}")

 

@app.event("message")

def debug_all_messages(event, logger):

    logger.info(f"DEBUG MESSAGE EVENT: {event}")

 

if __name__ == "__main__":

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])

    handler.start()
