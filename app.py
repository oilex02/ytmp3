import os
import re
import shutil
import tempfile
import zipfile
import uuid
import json
import threading
import time
from time import time as now
from flask import (
    Flask, request, jsonify, send_file, after_this_request,
    render_template_string, Response, stream_with_context
)
from yt_dlp import YoutubeDL

API_KEY = os.environ.get('API_KEY')  # Optional: set API key

app = Flask(__name__)

# -----------------------
# Configuration
# -----------------------
AUTO_CLEANUP_SECONDS = 10 * 60  # 10 minutes

# -----------------------
# In-memory job registry (token -> {path, filename, expires_at})
# For a single-server development environment only.
# -----------------------
JOBS = {}
JOB_LOCK = threading.Lock()


def schedule_job_cleanup(token: str, delay: int = AUTO_CLEANUP_SECONDS):
    def _cleanup():
        with JOB_LOCK:
            job = JOBS.pop(token, None)
        if job:
            try:
                shutil.rmtree(os.path.dirname(job['path']), ignore_errors=True)
                app.logger.info("Auto-cleaned job %s", token)
            except Exception:
                app.logger.exception("Error cleaning job %s", token)

    t = threading.Timer(delay, _cleanup)
    t.daemon = True
    t.start()


# -----------------------
# Utilities
# -----------------------
def is_youtube_url(url: str) -> bool:
    if not url:
        return False
    url = url.lower()
    return 'youtube.com/watch' in url or 'youtube.com/playlist' in url or 'youtu.be/' in url


def sanitize_filename(name: str, max_length: int = 200) -> str:
    if not name:
        return "untitled"
    name = re.sub(r'[\\/:*?"<>|]+', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    if len(name) > max_length:
        name = name[:max_length].rstrip()
    return name or "untitled"


# -----------------------
# Professional HTML front-end (uses SSE)
# -----------------------
HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>YouTube → MP3</title>
  <style>
    :root {
      --bg1: #0f172a;
      --accent: #00d4ff;
      --card: #0b1220;
      --muted: #9aa4b2;
    }
    html,body { height:100%; margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial; background: linear-gradient(135deg,#071024 0%, #001428 100%); color: #e6eef6; }
    .wrap { min-height:100%; display:flex; align-items:center; justify-content:center; padding:32px; box-sizing:border-box; }
    .card {
      width:100%; max-width:820px; background: linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.02));
      border-radius:14px; box-shadow: 0 10px 30px rgba(2,6,23,0.6); padding:28px; box-sizing:border-box;
      display:grid; grid-template-columns: 1fr 320px; gap:20px; align-items:start;
    }
    .brand { display:flex; gap:14px; align-items:center; }
    .logo {
      width:56px; height:56px; border-radius:10px; background: linear-gradient(135deg, var(--accent), #5d00ff); display:flex; align-items:center; justify-content:center; font-weight:700; color:#001022;
      box-shadow: 0 6px 18px rgba(0,0,0,0.6);
    }
    h1 { margin:0; font-size:20px; color:#fff; }
    p.lead { margin:8px 0 18px 0; color:var(--muted); font-size:14px; }
    .controls { display:flex; gap:12px; align-items:center; }
    input[type="text"] {
      flex:1; padding:12px 14px; border-radius:10px; border:1px solid rgba(255,255,255,0.06); background:rgba(255,255,255,0.02); color:#eaf6ff;
      outline:none; font-size:14px;
    }
    input::placeholder { color: rgba(230,238,246,0.35); }
    button.primary {
      padding:11px 16px; border-radius:10px; border:none; background:linear-gradient(90deg,var(--accent),#6a00ff); color:#021026; font-weight:700; cursor:pointer;
      box-shadow: 0 6px 18px rgba(0,212,255,0.08);
    }
    .right {
      background: rgba(255,255,255,0.01); border-radius:10px; padding:18px; height:100%;
    }
    .status { font-size:13px; color:var(--muted); margin-bottom:12px; }
    .progress {
      width:100%; height:12px; background: rgba(255,255,255,0.04); border-radius:8px; overflow:hidden; position:relative;
    }
    .progress > i { display:block; height:100%; width:0%; background: linear-gradient(90deg,var(--accent), #6a00ff); transition: width 300ms ease; }
    .messages { margin-top:12px; font-size:13px; color:#cfeffc; min-height:48px; max-height:180px; overflow:auto; padding-right:6px; }
    .msg { margin-bottom:8px; opacity:0.95; }
    a.download {
      display:inline-block; margin-top:12px; padding:10px 14px; border-radius:8px; background:#032237; color:var(--accent); border:1px solid rgba(0,212,255,0.08); text-decoration:none; font-weight:700;
    }
    .muted { color:var(--muted); font-size:12px; margin-top:12px; display:block; }
    @media (max-width:820px) {
      .card { grid-template-columns: 1fr; }
      .right { order:2; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card" role="application">
      <div>
        <div class="brand">
          <div class="logo">YT</div>
          <div>
            <h1>YouTube → MP3</h1>
            <div class="muted">Convert videos or playlists to MP3 quickly (local server)</div>
          </div>
        </div>

        <p class="lead">Paste a YouTube link and click <strong>Start</strong>. The page will show live progress and let you download the file when ready.</p>

        <div class="controls">
          <input id="youtubeLink" type="text" placeholder="Paste YouTube link, e.g. https://www.youtube.com/watch?v=..." />
          <button class="primary" id="startBtn">Start</button>
        </div>

        <div style="margin-top:14px; color:var(--muted); font-size:13px;">
          Tip: For playlists, the final download will be a ZIP file.
        </div>
      </div>

      <div class="right">
        <div class="status" id="statusText">Idle</div>
        <div class="progress" aria-hidden="true"><i id="bar"></i></div>
        <div class="messages" id="messages" aria-live="polite"></div>
        <div id="downloadArea"></div>
        <div class="muted">Powered by yt-dlp • Running locally</div>
      </div>
    </div>
  </div>

<script>
(function(){
  const startBtn = document.getElementById('startBtn');
  const linkInput = document.getElementById('youtubeLink');
  const messages = document.getElementById('messages');
  const statusText = document.getElementById('statusText');
  const bar = document.getElementById('bar');
  const downloadArea = document.getElementById('downloadArea');
  let es = null;

  function addMsg(text) {
    const el = document.createElement('div'); el.className = 'msg'; el.textContent = text;
    messages.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
  }

  function resetUI() {
    messages.innerHTML = '';
    bar.style.width = '0%';
    statusText.textContent = 'Idle';
    downloadArea.innerHTML = '';
    if (es) { es.close(); es = null; }
  }

  startBtn.addEventListener('click', () => {
    const url = linkInput.value.trim();
    if (!url) { alert('Please enter a YouTube link.'); return; }
    resetUI();
    statusText.textContent = 'Connecting...';

    // open SSE connection
    const p = `/progress?url=${encodeURIComponent(url)}`;
    es = new EventSource(p);

    es.addEventListener('open', () => {
      statusText.textContent = 'Starting...';
      addMsg('Connected. Starting download...');
    });

    es.addEventListener('progress', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.percent != null && !isNaN(data.percent)) {
          const pct = Math.min(100, Math.max(0, data.percent));
          bar.style.width = pct + '%';
          statusText.textContent = `Downloading — ${pct.toFixed(1)}%`;
        } else if (data.status) {
          addMsg(data.status);
        }
      } catch (e) {
        addMsg(ev.data);
      }
    });

    es.addEventListener('message', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        addMsg(JSON.stringify(data));
      } catch (e) {
        addMsg(ev.data);
      }
    });

    es.addEventListener('done', (ev) => {
      const d = JSON.parse(ev.data);
      addMsg('Finished: ' + (d.filename || d.token));
      statusText.textContent = 'Ready';
      bar.style.width = '100%';

      // show download link
      const a = document.createElement('a');
      a.className = 'download';
      a.href = `/download/${encodeURIComponent(d.token)}`;
      a.textContent = 'Download ' + (d.filename || 'file');
      downloadArea.appendChild(a);

      setTimeout(() => {
        if (es) { es.close(); es = null; }
      }, 2000);
    });

    es.addEventListener('error', (ev) => {
      let msg = 'An error occurred';
      try {
        if (ev.data) {
          const d = JSON.parse(ev.data);
          msg = d.error || JSON.stringify(d);
        }
      } catch (e) {}
      addMsg('Error: ' + msg);
      statusText.textContent = 'Error';
      if (es) { es.close(); es = null; }
    });
  });

})();
</script>
</body>
</html>
"""


# -----------------------
# SSE endpoint (/progress)
# Runs yt-dlp in a background thread and streams progress
# -----------------------
@app.route('/progress')
def progress_sse():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'missing url parameter'}), 400

    # API key enforcement (if configured)
    if API_KEY:
        key = request.headers.get('X-API-Key')
        if not key:
            return jsonify({'error': 'missing X-API-Key header'}), 403
        if key != API_KEY:
            return jsonify({'error': 'invalid api key'}), 403

    if not is_youtube_url(url):
        return jsonify({'error': 'unsupported url domain'}), 400

    def sse_event(name: str, data):
        payload = json.dumps(data)
        return f"event: {name}\ndata: {payload}\n\n"

    def generate():
        tempdir = tempfile.mkdtemp(prefix='ydl_')
        token = str(uuid.uuid4())

        # queue for events
        event_queue = []
        queue_lock = threading.Lock()
        finished = {'done': False}  # mutable flag
        result = {'success': False, 'final_path': None, 'final_name': None, 'error': None}

        # yt-dlp options
        ydl_opts_base = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(tempdir, '%(title)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }
            ],
            'logger': app.logger,
            'nocheckcertificate': True,
        }

        def push_event(name, data):
            with queue_lock:
                event_queue.append(sse_event(name, data))

        def progress_hook(d):
            try:
                status = d.get('status')
                if status == 'downloading':
                    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                    downloaded = d.get('downloaded_bytes') or 0
                    percent = (downloaded / total * 100) if total else None
                    evt = {
                        'status': 'downloading',
                        'percent': percent,
                        'speed': d.get('speed'),
                        'eta': d.get('eta'),
                        'filename': d.get('filename')
                    }
                    push_event('progress', evt)
                elif status == 'finished':
                    push_event('progress', {'status': 'download finished, converting...'})
            except Exception:
                app.logger.exception("progress_hook error")

        def run_download():
            try:
                with YoutubeDL(dict(ydl_opts_base, progress_hooks=[progress_hook])) as ydl:
                    info = ydl.extract_info(url, download=True)

                    # Prepare final deliverable
                    if 'entries' in info and info['entries']:
                        playlist_title = sanitize_filename(info.get('title') or 'playlist')
                        zip_path = os.path.join(tempdir, f"{playlist_title}.zip")
                        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zipf:
                            for entry in info['entries']:
                                if not entry:
                                    continue
                                entry_title = sanitize_filename(entry.get('title') or entry.get('id') or 'untitled')
                                mp3_filename = os.path.join(tempdir, f"{entry_title}.mp3")
                                if os.path.exists(mp3_filename):
                                    zipf.write(mp3_filename, arcname=f"{entry_title}.mp3")
                                else:
                                    for fname in os.listdir(tempdir):
                                        if fname.lower().endswith('.mp3'):
                                            if (entry.get('id') and entry['id'] in fname) or (entry_title in fname):
                                                zipf.write(os.path.join(tempdir, fname), arcname=fname)
                                                break
                        final_path = zip_path
                        final_name = f"{playlist_title}.zip"
                    else:
                        title = sanitize_filename(info.get('title') or info.get('id') or 'video')
                        mp3_path = os.path.join(tempdir, f"{title}.mp3")
                        if not os.path.exists(mp3_path):
                            mp3s = [f for f in os.listdir(tempdir) if f.lower().endswith('.mp3')]
                            if len(mp3s) == 1:
                                mp3_path = os.path.join(tempdir, mp3s[0])
                            elif len(mp3s) > 1:
                                match = next((f for f in mp3s if title in f), mp3s[0])
                                mp3_path = os.path.join(tempdir, match)
                            else:
                                raise RuntimeError("expected mp3 file not found after download")
                        final_path = mp3_path
                        final_name = os.path.basename(mp3_path)

                    # register job
                    with JOB_LOCK:
                        JOBS[token] = {'path': final_path, 'filename': final_name, 'expires_at': now() + AUTO_CLEANUP_SECONDS}
                    schedule_job_cleanup(token)

                    result['success'] = True
                    result['final_path'] = final_path
                    result['final_name'] = final_name

            except Exception as exc:
                app.logger.exception("SSE download error")
                result['error'] = str(exc)
            finally:
                finished['done'] = True
                # Ensure at least one event gets pushed so generate loop can observe finished state
                push_event('progress', {'status': 'worker finished'})

        # start background worker
        worker = threading.Thread(target=run_download, daemon=True)
        worker.start()

        # stream events while worker runs and until queue drained
        try:
            while not finished['done'] or event_queue:
                # flush queue
                to_yield = None
                with queue_lock:
                    if event_queue:
                        # pop first event
                        to_yield = event_queue.pop(0)
                if to_yield:
                    yield to_yield
                else:
                    # keep-alive comment to prevent some proxies closing connection
                    yield ": keep-alive\n\n"
                    time.sleep(0.25)

            # worker finished; check result
            if result['success']:
                yield sse_event('done', {'token': token, 'filename': result['final_name']})
            else:
                yield sse_event('error', {'error': result['error'] or 'unknown error'})
                # cleanup tempdir on failure
                try:
                    shutil.rmtree(tempdir, ignore_errors=True)
                except Exception:
                    pass

        except GeneratorExit:
            # client disconnected
            app.logger.info("SSE client disconnected")
            # don't try to remove files here; worker may still be running
            return
        except Exception:
            app.logger.exception("Exception in SSE generator")
            try:
                yield sse_event('error', {'error': 'internal server error in progress stream'})
            except Exception:
                pass

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


# -----------------------
# One-shot download route for completed jobs
# -----------------------
@app.route('/download/<token>')
def download_token(token):
    with JOB_LOCK:
        job = JOBS.pop(token, None)

    if not job:
        return jsonify({'error': 'invalid or expired token'}), 404

    filepath = job['path']
    filename = job.get('filename') or os.path.basename(filepath)

    try:
        @after_this_request
        def remove_file(response):
            try:
                shutil.rmtree(os.path.dirname(filepath), ignore_errors=True)
            except Exception:
                app.logger.exception("cleanup after download failed")
            return response
    except Exception:
        app.logger.exception("couldn't register after_this_request cleanup")

    return send_file(filepath, as_attachment=True, download_name=filename)


# -----------------------
# Your original /fetch route (kept intact)
# -----------------------
@app.route('/fetch')
def fetch():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'missing url parameter'}), 400

    if API_KEY:
        key = request.headers.get('X-API-Key')
        if not key:
            return jsonify({'error': 'missing X-API-Key header'}), 403
        if key != API_KEY:
            return jsonify({'error': 'invalid api key'}), 403

    if not is_youtube_url(url):
        return jsonify({'error': 'unsupported url domain'}), 400

    tempdir = tempfile.mkdtemp(prefix='ydl_')

    @after_this_request
    def cleanup(response):
        try:
            shutil.rmtree(tempdir)
        except Exception as e:
            app.logger.error("cleanup error: %s", e)
        return response

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(tempdir, '%(title)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'postprocessors': [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }
        ],
        'logger': app.logger,
        'nocheckcertificate': True,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            if 'entries' in info and info['entries']:
                playlist_title = sanitize_filename(info.get('title') or 'playlist')
                zip_path = os.path.join(tempdir, f"{playlist_title}.zip")
                with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zipf:
                    for entry in info['entries']:
                        if not entry:
                            continue
                        entry_title = sanitize_filename(entry.get('title') or entry.get('id') or 'untitled')
                        mp3_filename = os.path.join(tempdir, f"{entry_title}.mp3")
                        if os.path.exists(mp3_filename):
                            zipf.write(mp3_filename, arcname=f"{entry_title}.mp3")
                        else:
                            for fname in os.listdir(tempdir):
                                if fname.lower().endswith('.mp3'):
                                    if (entry.get('id') and entry['id'] in fname) or (entry_title in fname):
                                        zipf.write(os.path.join(tempdir, fname), arcname=fname)
                                        break
                return send_file(
                    zip_path,
                    as_attachment=True,
                    download_name=f"{playlist_title}.zip",
                    mimetype='application/zip'
                )

            else:
                title = sanitize_filename(info.get('title') or info.get('id') or 'video')
                mp3_path = os.path.join(tempdir, f"{title}.mp3")
                if not os.path.exists(mp3_path):
                    mp3s = [f for f in os.listdir(tempdir) if f.lower().endswith('.mp3')]
                    if len(mp3s) == 1:
                        mp3_path = os.path.join(tempdir, mp3s[0])
                    elif len(mp3s) > 1:
                        match = next((f for f in mp3s if title in f), mp3s[0])
                        mp3_path = os.path.join(tempdir, match)
                    else:
                        raise RuntimeError("expected mp3 file not found after download")

                return send_file(
                    mp3_path,
                    as_attachment=True,
                    download_name=f"{title}.mp3",
                    mimetype='audio/mpeg'
                )

    except Exception as e:
        shutil.rmtree(tempdir, ignore_errors=True)
        app.logger.exception("download error")
        return jsonify({'error': str(e)}), 500


# -----------------------
# Root route to serve the frontend
# -----------------------
@app.route('/')
def index():
    return render_template_string(HTML_PAGE)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
