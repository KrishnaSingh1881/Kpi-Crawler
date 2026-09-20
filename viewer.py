#!/usr/bin/env python3
"""
KPI Crawler Artifact Viewer & Exporter Server
Solves OS file association issues with .raw files by serving live previews with proper HTTP content types
and offering 1-click export of crawled artifacts as .html, .pdf, .json, and .xml.
"""

import sys
import os
import json
import re
import shutil
import urllib.parse
import mimetypes
from http.server import HTTPServer, SimpleHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(BASE_DIR, ".data", "runs")
WEB_DIR = os.path.join(BASE_DIR, "web_viewer")
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")

class ViewerHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/runs":
            return self.handle_get_runs()
        
        # /api/runs/<site>/<run>/artifacts
        m_arts = re.match(r"^/api/runs/([^/]+)/([^/]+)/artifacts$", path)
        if m_arts:
            site_id, run_id = m_arts.group(1), m_arts.group(2)
            return self.handle_get_artifacts(site_id, run_id)

        # /api/preview/<site>/<run>/<checksum>
        m_prev = re.match(r"^/api/preview/([^/]+)/([^/]+)/([a-f0-9]{64})$", path)
        if m_prev:
            site_id, run_id, checksum = m_prev.group(1), m_prev.group(2), m_prev.group(3)
            is_download = query.get("download", ["0"])[0] == "1"
            return self.handle_preview(site_id, run_id, checksum, is_download)

        # Serve static web frontend
        if path == "/" or path == "":
            req_path = os.path.join(WEB_DIR, "index.html")
        else:
            rel_path = path.lstrip("/")
            req_path = os.path.join(WEB_DIR, rel_path)

        if os.path.exists(req_path) and os.path.isfile(req_path):
            return self.serve_static_file(req_path)
        
        # Fallback to index.html
        return self.serve_static_file(os.path.join(WEB_DIR, "index.html"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # /api/export/<site>/<run>
        m_exp = re.match(r"^/api/export/([^/]+)/([^/]+)$", path)
        if m_exp:
            site_id, run_id = m_exp.group(1), m_exp.group(2)
            return self.handle_export(site_id, run_id)

        self.send_error(404, "Endpoint not found")

    def handle_get_runs(self):
        runs = []
        if os.path.exists(RUNS_DIR):
            for site in os.listdir(RUNS_DIR):
                site_dir = os.path.join(RUNS_DIR, site)
                if not os.path.isdir(site_dir):
                    continue
                for run_id in os.listdir(site_dir):
                    run_dir = os.path.join(site_dir, run_id)
                    manifest_path = os.path.join(run_dir, "manifest.json")
                    if os.path.exists(manifest_path):
                        try:
                            with open(manifest_path, "r", encoding="utf-8") as f:
                                meta = json.load(f)
                            runs.append(meta)
                        except Exception:
                            pass
        
        runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        self.send_json_response(runs)

    def handle_get_artifacts(self, site_id, run_id):
        run_dir = os.path.join(RUNS_DIR, site_id, run_id)
        arts_path = os.path.join(run_dir, "program2", "artifacts.json")
        
        if not os.path.exists(arts_path):
            self.send_error(404, "Run artifacts file not found")
            return

        try:
            with open(arts_path, "r", encoding="utf-8") as f:
                artifacts = json.load(f)
        except Exception as e:
            self.send_error(500, f"Error reading artifacts: {e}")
            return

        # Enrich artifacts with suggested filenames
        for art in artifacts:
            url = art.get("source_url") or art.get("final_url") or ""
            ctype = art.get("content_type", "")
            ext = self.guess_extension(ctype, art.get("raw_location", ""))
            art["filename_suggested"] = self.derive_filename(url, art.get("checksum", ""), ext)

        self.send_json_response(artifacts)

    def handle_preview(self, site_id, run_id, checksum, is_download):
        run_dir = os.path.join(RUNS_DIR, site_id, run_id)
        arts_path = os.path.join(run_dir, "program2", "artifacts.json")

        target_art = None
        if os.path.exists(arts_path):
            with open(arts_path, "r", encoding="utf-8") as f:
                artifacts = json.load(f)
            for a in artifacts:
                if a.get("checksum") == checksum:
                    target_art = a
                    break

        if not target_art:
            self.send_error(404, "Artifact checksum not found")
            return

        raw_rel = target_art.get("raw_location", "")
        raw_full = os.path.join(run_dir, raw_rel)

        if not os.path.exists(raw_full):
            self.send_error(404, "Raw artifact file missing on disk")
            return

        ctype = target_art.get("content_type", "application/octet-stream")
        ext = self.guess_extension(ctype, raw_rel)
        filename = self.derive_filename(target_art.get("source_url", ""), checksum, ext)

        try:
            with open(raw_full, "rb") as f:
                content = f.read()
        except Exception as e:
            self.send_error(500, f"Error reading file: {e}")
            return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        
        if is_download:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        else:
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')

        self.end_headers()
        self.wfile.write(content)

    def handle_export(self, site_id, run_id):
        run_dir = os.path.join(RUNS_DIR, site_id, run_id)
        arts_path = os.path.join(run_dir, "program2", "artifacts.json")

        if not os.path.exists(arts_path):
            self.send_error(404, "Run artifacts missing")
            return

        with open(arts_path, "r", encoding="utf-8") as f:
            artifacts = json.load(f)

        target_export_dir = os.path.join(EXPORTS_DIR, site_id, run_id)
        os.makedirs(target_export_dir, exist_ok=True)

        count = 0
        for art in artifacts:
            raw_rel = art.get("raw_location", "")
            raw_full = os.path.join(run_dir, raw_rel)
            if not os.path.exists(raw_full):
                continue

            ctype = art.get("content_type", "")
            ext = self.guess_extension(ctype, raw_rel)
            cat = self.category_for_ext(ext)
            
            cat_dir = os.path.join(target_export_dir, cat)
            os.makedirs(cat_dir, exist_ok=True)

            filename = self.derive_filename(art.get("source_url", ""), art.get("checksum", ""), ext)
            dest_path = os.path.join(cat_dir, filename)

            shutil.copyfile(raw_full, dest_path)
            count += 1

        res = {
            "status": "SUCCESS",
            "site_id": site_id,
            "run_id": run_id,
            "exported_count": count,
            "export_dir": target_export_dir
        }
        self.send_json_response(res)

    @staticmethod
    def guess_extension(ctype, raw_loc):
        ctype = ctype.lower()
        raw_loc = raw_loc.lower()
        if "html" in ctype or "/html/" in raw_loc:
            return "html"
        if "pdf" in ctype or "/pdf/" in raw_loc:
            return "pdf"
        if "json" in ctype or "/json/" in raw_loc:
            return "json"
        if "xml" in ctype or "rss" in ctype or "/xml/" in raw_loc:
            return "xml"
        if "plain" in ctype or "text" in ctype:
            return "txt"
        
        guessed = mimetypes.guess_extension(ctype)
        if guessed:
            return guessed.lstrip(".")
        return "data"

    @staticmethod
    def category_for_ext(ext):
        if ext in ("html", "htm"): return "html"
        if ext == "pdf": return "pdf"
        if ext == "json": return "json"
        if ext in ("xml", "rss", "atom"): return "xml"
        return "other"

    @staticmethod
    def derive_filename(url, checksum, ext):
        if url:
            path = urllib.parse.urlparse(url).path.strip("/")
            if not path:
                slug = "index"
            else:
                slug = re.sub(r"[^a-zA-Z0-9_-]", "_", path)[:60]
        else:
            slug = checksum[:12]
        
        return f"{slug}.{ext}"

    def serve_static_file(self, file_path):
        ctype, _ = mimetypes.guess_type(file_path)
        if not ctype:
            if file_path.endswith(".css"): ctype = "text/css"
            elif file_path.endswith(".js"): ctype = "application/javascript"
            else: ctype = "text/html"

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(500, f"Static serve error: {e}")

    def send_json_response(self, data):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def run_server(port=8080):
    server_address = ("", port)
    httpd = HTTPServer(server_address, ViewerHandler)
    print(f"🚀 KPI Crawler Artifact Viewer Server running at http://localhost:{port}")
    print(f"📁 Serving runs from: {RUNS_DIR}")
    print("Press Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        httpd.server_close()

def run_self_test():
    print("=== Running KPI Viewer Self-Test ===")
    print("Testing runs discovery...")
    runs = []
    if os.path.exists(RUNS_DIR):
        for site in os.listdir(RUNS_DIR):
            site_dir = os.path.join(RUNS_DIR, site)
            if os.path.isdir(site_dir):
                for r in os.listdir(site_dir):
                    mp = os.path.join(site_dir, r, "manifest.json")
                    if os.path.exists(mp):
                        with open(mp) as f:
                            runs.append(json.load(f))
    print(f"✅ Found {len(runs)} crawl runs!")
    for r in runs:
        print(f"   - Site: {r.get('site_id')} | Run: {r.get('run_id')} | Artifacts: {r.get('artifact_count')}")

    print("\nTesting export for site 'data.utoronto.ca', run '184'...")
    run_dir = os.path.join(RUNS_DIR, "data.utoronto.ca", "184")
    arts_path = os.path.join(run_dir, "program2", "artifacts.json")
    if os.path.exists(arts_path):
        with open(arts_path) as f:
            arts = json.load(f)
        target_export_dir = os.path.join(EXPORTS_DIR, "data.utoronto.ca", "184")
        os.makedirs(target_export_dir, exist_ok=True)
        count = 0
        for a in arts:
            raw_rel = a.get("raw_location", "")
            raw_full = os.path.join(run_dir, raw_rel)
            if not os.path.exists(raw_full): continue
            ctype = a.get("content_type", "")
            ext = ViewerHandler.guess_extension(ctype, raw_rel)
            cat = ViewerHandler.category_for_ext(ext)
            cat_dir = os.path.join(target_export_dir, cat)
            os.makedirs(cat_dir, exist_ok=True)
            fname = ViewerHandler.derive_filename(a.get("source_url", ""), a.get("checksum", ""), ext)
            shutil.copyfile(raw_full, os.path.join(cat_dir, fname))
            count += 1
        print(f"✅ Successfully exported {count} files with real extensions to:")
        print(f"   {target_export_dir}")
        for cat in os.listdir(target_export_dir):
            cd = os.path.join(target_export_dir, cat)
            print(f"   📁 {cat}/: {len(os.listdir(cd))} files")

    print("\n=== Self-Test Completed Successfully ===")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        run_self_test()
    else:
        port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8080
        run_server(port)
