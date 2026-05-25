import redis
import json
import os
import threading
import yt_dlp
import datetime
import imageio_ffmpeg
from urllib.parse import urlparse

# =========================
# CONFIG
# =========================

MAX_DURATION_SECONDS = 20 * 60  # 20 mins
MAX_FILESIZE_MB = 45
MAX_FILESIZE_BYTES = MAX_FILESIZE_MB * 1024 * 1024

DOWNLOAD_TIMEOUT = 120

ALLOWED_DOMAINS = [
    'youtube.com',
    'www.youtube.com',
    'youtu.be',
]

DOWNLOAD_QUEUE = 'download:queue'

# Keep this LOW initially
NUM_DOWNLOAD_WORKERS = 2

# =========================
# REDIS
# =========================

redis_url = urlparse(
    os.getenv('REDIS_URL', 'redis://localhost:6379')
)

r = redis.Redis(
    host=redis_url.hostname,
    port=redis_url.port,
)

pubsub_client = r.pubsub()

pubsub_client.subscribe(
    'meta:request',
    'search:request',
    'download:start'
)

print(f"Worker listening with {NUM_DOWNLOAD_WORKERS} workers...")

# =========================
# HELPERS
# =========================

def validate_url(url: str):
    parsed = urlparse(url)

    if parsed.netloc.lower() not in ALLOWED_DOMAINS:
        raise Exception("Unsupported domain.")


def make_progress_hook(redis_client, chat_id):
    last_percent = [-1]

    def hook(d):
        if d['status'] == 'downloading':
            percent_str = d.get('_percent_str', '0%').strip()
            speed_str = d.get('_speed_str', 'N/A').strip()
            eta_str = d.get('_eta_str', 'N/A').strip()

            try:
                percent = int(float(percent_str.replace('%', '')))
            except:
                return

            if percent - last_percent[0] >= 10:
                last_percent[0] = percent

                progress = (
                    '█' * (percent // 10)
                    + '░' * (10 - percent // 10)
                )

                redis_client.publish(
                    'download:progress',
                    json.dumps({
                        'chatId': chat_id,
                        'text': (
                            f"⬇️ [{progress}] {percent}%\n"
                            f"⚡ {speed_str} | ⏱ ETA: {eta_str}"
                        )
                    })
                )

        elif d['status'] == 'finished':
            redis_client.publish(
                'download:progress',
                json.dumps({
                    'chatId': chat_id,
                    'text': '✅ Download complete, uploading...'
                })
            )

    return hook


# =========================
# METADATA
# =========================

def get_metadata(url: str) -> dict:
    validate_url(url)

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

        duration = info.get('duration', 0)

        filesize = (
            info.get('filesize')
            or info.get('filesize_approx')
            or 0
        )

        return {
            'title': info.get('title', 'Unknown'),
            'thumbnail': info.get('thumbnail', ''),
            'duration': info.get('duration_string', ''),
            'durationSeconds': duration,
            'filesize': filesize,
        }


# =========================
# DOWNLOAD
# =========================

def download_video(url: str, quality: str, chat_id: str):
    validate_url(url)

    meta = get_metadata(url)

    # Duration check
    if meta['durationSeconds'] > MAX_DURATION_SECONDS:
        raise Exception(
            f"Video exceeds {MAX_DURATION_SECONDS // 60} minute limit."
        )

    # Filesize pre-check
    if (
        meta['filesize']
        and meta['filesize'] > MAX_FILESIZE_BYTES
    ):
        raise Exception(
            f"Video exceeds {MAX_FILESIZE_MB}MB limit."
        )

    base_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'downloads'
    )

    os.makedirs(base_dir, exist_ok=True)

    timestamp = datetime.datetime.now().strftime(
        '%Y-%m-%d_%H-%M-%S'
    )

    if quality == 'mp3':
        output_template = os.path.join(
            base_dir,
            f"{chat_id}-{timestamp}"
        )

        output_path = output_template + '.mp3'

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_template,
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
            'socket_timeout': DOWNLOAD_TIMEOUT,
            'progress_hooks': [
                make_progress_hook(r, chat_id)
            ],
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
        }

        file_type = 'audio'

    else:
        output_path = os.path.join(
            base_dir,
            f"{chat_id}-{timestamp}.mp4"
        )

        ydl_opts = {
            'format': (
                f'bestvideo[height<={quality}]'
                f'+bestaudio/'
                f'best[height<={quality}]'
            ),
            'outtmpl': output_path,
            'merge_output_format': 'mp4',
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
            'socket_timeout': DOWNLOAD_TIMEOUT,
            'progress_hooks': [
                make_progress_hook(r, chat_id)
            ],
            'quiet': True,
        }

        file_type = 'video'

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # REAL filesize validation
    actual_size = os.path.getsize(output_path)

    if actual_size > MAX_FILESIZE_BYTES:
        os.remove(output_path)

        raise Exception(
            f"Downloaded file exceeds {MAX_FILESIZE_MB}MB limit."
        )

    return output_path, file_type


# =========================
# HANDLERS
# =========================

def handle_meta(url, chat_id):
    print(f"[META] Fetching info for: {url}")

    try:
        meta = get_metadata(url)

        if meta['durationSeconds'] > MAX_DURATION_SECONDS:
            r.publish(
                f'meta:response:{chat_id}',
                json.dumps({
                    'chatId': chat_id,
                    'error': (
                        f'❌ Video exceeds '
                        f'{MAX_DURATION_SECONDS // 60} minute limit.'
                    )
                })
            )
            return

        if (
            meta['filesize']
            and meta['filesize'] > MAX_FILESIZE_BYTES
        ):
            r.publish(
                f'meta:response:{chat_id}',
                json.dumps({
                    'chatId': chat_id,
                    'error': (
                        f'❌ Video exceeds '
                        f'{MAX_FILESIZE_MB}MB limit.'
                    )
                })
            )
            return

        r.publish(
            f'meta:response:{chat_id}',
            json.dumps({
                'chatId': chat_id,
                **meta
            })
        )

    except Exception as e:
        print(f"[META] Error: {e}")

        r.publish(
            f'meta:response:{chat_id}',
            json.dumps({
                'chatId': chat_id,
                'error': str(e)
            })
        )


def handle_search(query, chat_id):
    print(f"[SEARCH] Query: {query}")

    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            results = ydl.extract_info(
                f"ytsearch10:{query}",
                download=False
            )

        items = []

        for entry in results.get('entries', []):
            items.append({
                'title': entry.get('title', 'Unknown'),
                'url': (
                    entry.get('url')
                    or f"https://www.youtube.com/watch?v={entry.get('id')}"
                ),
                'duration': entry.get('duration_string', ''),
                'thumbnail': entry.get('thumbnail', ''),
            })

        r.publish(
            f'search:response:{chat_id}',
            json.dumps({
                'chatId': chat_id,
                'query': query,
                'items': items
            })
        )

    except Exception as e:
        print(f"[SEARCH] Error: {e}")

        r.publish(
            f'search:response:{chat_id}',
            json.dumps({
                'chatId': chat_id,
                'error': str(e)
            })
        )


def handle_download_request(url, quality, chat_id):
    r.lpush(
        DOWNLOAD_QUEUE,
        json.dumps({
            'url': url,
            'quality': quality,
            'chatId': chat_id
        })
    )

    queue_length = r.llen(DOWNLOAD_QUEUE)

    if queue_length > 1:
        r.publish(
            'download:progress',
            json.dumps({
                'chatId': chat_id,
                'text': (
                    f'📋 You are #{queue_length} in queue...'
                )
            })
        )


# =========================
# DOWNLOAD WORKER
# =========================

def download_worker():
    worker_r = redis.Redis(
        host=redis_url.hostname,
        port=redis_url.port
    )

    while True:
        try:
            job = worker_r.brpop(
                DOWNLOAD_QUEUE,
                timeout=0
            )

            if not job:
                continue

            data = json.loads(job[1])

            url = data['url']
            quality = data['quality']
            chat_id = data['chatId']

            print(f"[DOWNLOAD] {quality} -> {url}")

            worker_r.publish(
                'download:progress',
                json.dumps({
                    'chatId': chat_id,
                    'text': (
                        f'⏳ Download started at {quality}...'
                    )
                })
            )

            try:
                file_path, file_type = download_video(
                    url,
                    quality,
                    chat_id
                )

                worker_r.publish(
                    'download:done',
                    json.dumps({
                        'chatId': chat_id,
                        'filePath': file_path,
                        'fileType': file_type
                    })
                )

            except Exception as e:
                print(f"[DOWNLOAD] Error: {e}")

                worker_r.publish(
                    'download:done',
                    json.dumps({
                        'chatId': chat_id,
                        'error': str(e)
                    })
                )

        except Exception as e:
            print(f"[WORKER] Unexpected error: {e}")


# =========================
# START WORKERS
# =========================

for i in range(NUM_DOWNLOAD_WORKERS):
    t = threading.Thread(
        target=download_worker,
        daemon=True
    )

    t.start()

    print(f"[WORKER] Worker {i + 1} started")


# =========================
# PUBSUB LOOP
# =========================

for message in pubsub_client.listen():
    if message['type'] != 'message':
        continue

    channel = message['channel'].decode()

    data = json.loads(message['data'])

    chat_id = data['chatId']

    if channel == 'meta:request':
        threading.Thread(
            target=handle_meta,
            args=(data['url'], chat_id),
            daemon=True
        ).start()

    elif channel == 'search:request':
        threading.Thread(
            target=handle_search,
            args=(data['query'], chat_id),
            daemon=True
        ).start()

    elif channel == 'download:start':
        handle_download_request(
            data['url'],
            data['quality'],
            chat_id
        )
