"""Real build-and-run test of every framework preset with Codeskys' generated Dockerfile.

For each of the 14 frameworks: write a minimal real app (pinned framework versions,
a unique marker in its UI), build it through a compose file shaped like composegen's
(build: context + dockerfile_inline, or the app's own Dockerfile for "docker"), run it
with the production hardening (cap_drop ALL, no-new-privileges, pids/memory limits),
and require HTTP 200 with the marker. Needs Docker and internet (npm, PyPI, images).

    python tests/e2e/framework_builds.py [framework ...]

Not collected by pytest (slow, needs Docker). Cleans up its images and containers.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kova import buildpolicy, composegen, dockerfiles  # noqa: E402
from kova.gitinfo import FRAMEWORKS  # noqa: E402


# The project's "Node version" setting; the UI default is 20.x.  CS_NODE_VERSION=22 ... to test others.
NODE_VERSION = os.environ.get("CS_NODE_VERSION", "20.x")


def pkg(scripts, deps=None, dev=None, **extra):
    return json.dumps({"name": "codeskys-fixture", "private": True, "version": "1.0.0", "scripts": scripts,
                       "dependencies": deps or {}, "devDependencies": dev or {}, **extra}, indent=1)


def html(marker, body=""):
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{marker}</title></head>"
            f"<body><h1>{marker}</h1>{body}</body></html>")


REACT18 = {"react": "18.3.1", "react-dom": "18.3.1"}


def fixtures(m: str) -> dict:
    """framework -> {path: content}. ``m`` is the marker every app must serve."""
    return {
        "static": {"index.html": html(m)},
        "react": {
            "package.json": pkg({"build": "vite build"}, REACT18, {"vite": "5.4.11", "@vitejs/plugin-react": "4.3.4"},
                                type="module"),
            "vite.config.js": "import react from '@vitejs/plugin-react';\nexport default { plugins: [react()] };\n",
            "index.html": html(m, '<div id="root"></div><script type="module" src="/src/main.jsx"></script>'),
            "src/main.jsx": "import { createRoot } from 'react-dom/client';\n"
                            f"createRoot(document.getElementById('root')).render(<p>{m} react</p>);\n"},
        "cra": {
            "package.json": pkg({"build": "react-scripts build"}, {**REACT18, "react-scripts": "5.0.1"},
                                browserslist=[">0.2%", "not dead"]),
            "public/index.html": html(m, '<div id="root"></div>'),
            "src/index.js": "import { createRoot } from 'react-dom/client';\n"
                            f"createRoot(document.getElementById('root')).render(<p>{m} cra</p>);\n"},
        "vue": {
            "package.json": pkg({"build": "vite build"}, {"vue": "3.5.13"}, {"vite": "5.4.11"}, type="module"),
            "index.html": html(m, '<div id="app"></div><script type="module" src="/src/main.js"></script>'),
            "src/main.js": "import { createApp, h } from 'vue';\n"
                           f"createApp({{ render: () => h('p', '{m} vue') }}).mount('#app');\n"},
        "nuxt": {
            "package.json": pkg({"build": "nuxt build"}, {"nuxt": "3.15.1", "vue": "3.5.13"}, type="module"),
            "nuxt.config.ts": "export default defineNuxtConfig({ telemetry: false, devtools: { enabled: false } });\n",
            "app.vue": f"<template><h1>{m}</h1></template>\n"},
        "svelte": {
            "package.json": pkg({"build": "vite build"}, {"@sveltejs/adapter-node": "5.2.11"},
                                {"@sveltejs/kit": "2.15.1", "@sveltejs/vite-plugin-svelte": "5.0.3",
                                 "svelte": "5.16.0", "vite": "6.0.7"}, type="module"),
            "svelte.config.js": "import adapter from '@sveltejs/adapter-node';\n"
                                "export default { kit: { adapter: adapter() } };\n",
            "vite.config.js": "import { sveltekit } from '@sveltejs/kit/vite';\n"
                              "export default { plugins: [sveltekit()] };\n",
            "src/app.html": "<!doctype html><html><head>%sveltekit.head%</head><body>%sveltekit.body%</body></html>\n",
            "src/routes/+page.svelte": f"<h1>{m}</h1>\n"},
        "angular": {
            "package.json": pkg({"build": "ng build"},
                                {"@angular/common": "19.0.5", "@angular/compiler": "19.0.5", "@angular/core": "19.0.5",
                                 "@angular/platform-browser": "19.0.5", "rxjs": "7.8.1", "tslib": "2.8.1",
                                 "zone.js": "0.15.0"},
                                {"@angular/build": "19.0.6", "@angular/cli": "19.0.6",
                                 "@angular/compiler-cli": "19.0.5", "typescript": "5.6.3"}),
            "angular.json": json.dumps({"version": 1, "newProjectRoot": "projects", "cli": {"analytics": False},
                                        "projects": {"fixture": {"projectType": "application", "root": "",
                                                                 "sourceRoot": "src", "architect": {"build": {
                                                                     "builder": "@angular/build:application",
                                                                     "options": {"outputPath": "dist/fixture",
                                                                                 "index": "src/index.html",
                                                                                 "browser": "src/main.ts",
                                                                                 "polyfills": ["zone.js"],
                                                                                 "tsConfig": "tsconfig.json"}}}}}}),
            "tsconfig.json": json.dumps({"compilerOptions": {"target": "ES2022", "module": "ES2022",
                                                             "moduleResolution": "bundler", "strict": True,
                                                             "experimentalDecorators": True, "lib": ["ES2022", "dom"]},
                                         "files": ["src/main.ts"]}),
            "src/index.html": html(m, "<app-root></app-root>"),
            "src/main.ts": "import { Component } from '@angular/core';\n"
                           "import { bootstrapApplication } from '@angular/platform-browser';\n"
                           f"@Component({{ selector: 'app-root', standalone: true, template: '<p>{m} angular</p>' }})\n"
                           "class App {}\nbootstrapApplication(App);\n"},
        "astro": {
            "package.json": pkg({"build": "astro build"}, {"astro": "5.1.3"}, type="module"),
            "src/pages/index.astro": f"<html><head><title>{m}</title></head><body><h1>{m}</h1></body></html>\n"},
        "gatsby": {
            "package.json": pkg({"build": "gatsby build"}, {**REACT18, "gatsby": "5.14.1"}),
            "gatsby-config.js": "module.exports = {};\n",
            "src/pages/index.js": f"import * as React from 'react';\nexport default function Index() {{ return <h1>{m}</h1>; }}\n"},
        "nextjs": {
            "package.json": pkg({"build": "next build", "start": "next start"},
                                {"next": "15.1.6", "react": "19.0.0", "react-dom": "19.0.0"}),
            "next.config.js": "module.exports = {};\n",
            "app/layout.js": "export default function L({ children }) { return <html><body>{children}</body></html>; }\n",
            "app/page.js": f"export default function P() {{ return <h1>{m}</h1>; }}\n"},
        "node": {
            "package.json": pkg({"start": "node server.js"}, {"express": "4.21.2"}),
            "server.js": "const app = require('express')();\n"
                         f"app.get('/', (q, r) => r.send('<h1>{m}</h1>'));\n"
                         "app.listen(process.env.PORT || 3000, '0.0.0.0');\n"},
        "flask": {
            "requirements.txt": "flask==3.1.0\ngunicorn==23.0.0\n",
            "app.py": f"from flask import Flask\napp = Flask(__name__)\n\n@app.get('/')\ndef index():\n    return '<h1>{m}</h1>'\n"},
        "django": {
            "requirements.txt": "django==5.1.4\ngunicorn==23.0.0\n",
            "manage.py": "import os, sys\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
                         "from django.core.management import execute_from_command_line\nexecute_from_command_line(sys.argv)\n",
            "config/__init__.py": "",
            "config/settings.py": "SECRET_KEY = 'fixture-only'\nDEBUG = False\nALLOWED_HOSTS = ['*']\n"
                                  "INSTALLED_APPS = ['django.contrib.staticfiles']\nROOT_URLCONF = 'config.urls'\n"
                                  "STATIC_URL = '/static/'\nSTATIC_ROOT = 'staticfiles'\nMIDDLEWARE = []\n",
            "config/urls.py": "from django.http import HttpResponse\nfrom django.urls import path\n"
                              f"urlpatterns = [path('', lambda r: HttpResponse('<h1>{m}</h1>'))]\n",
            "config/wsgi.py": "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
                              "from django.core.wsgi import get_wsgi_application\napplication = get_wsgi_application()\n"},
        "docker": {
            "Dockerfile": "FROM python:3.12-slim-bookworm\nRUN useradd -u 10001 app\nWORKDIR /srv\nCOPY index.html .\n"
                          "USER 10001\nEXPOSE 3000\nCMD [\"python\", \"-m\", \"http.server\", \"3000\"]\n",
            "index.html": html(m)},
    }


def sh(*args, timeout=1800):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def run_one(fw: str, work: Path) -> tuple[bool, str]:
    marker = f"CODESKYS-FW-{fw.upper()}-OK"
    app = work / fw
    for rel, content in fixtures(marker)[fw].items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_text(content, encoding="utf-8", newline="\n")
    if fw == "docker":
        problems = buildpolicy.dockerfile_problems((app / "Dockerfile").read_text())
        if problems:
            return False, f"policy refused the fixture Dockerfile: {problems}"
        build, port = {"context": f"./{fw}", "dockerfile": "Dockerfile"}, 3000
    else:
        text, port = dockerfiles.generate({"framework": fw, "node_version": NODE_VERSION})
        stored = composegen.compose_escape(text)            # exactly what composegen writes
        problem = composegen._inline_problem(stored)
        if problem:
            return False, f"generated Dockerfile refused by assert_safe: {problem}"
        build = {"context": f"./{fw}", "dockerfile_inline": stored}
    image = f"cs-fwtest-{fw}:latest"
    compose = work / f"compose-{fw}.json"
    compose.write_text(json.dumps({"name": f"cs-fwtest-{fw}", "services": {"app": {"build": build, "image": image}}}))
    t0 = time.time()
    b = sh("docker", "compose", "-f", str(compose), "--progress", "plain", "build", "app")
    if b.returncode != 0:
        tail = " | ".join(l for l in (b.stdout + b.stderr).splitlines() if l.strip())[-700:]
        return False, f"build failed after {time.time() - t0:.0f}s: {tail}"
    built = time.time() - t0
    name = f"cs-fwtest-{fw}-run"
    sh("docker", "rm", "-f", name)
    r = sh("docker", "run", "-d", "--name", name, "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
           "--pids-limit", "256", "--memory", "512m", "-e", f"PORT={port}", "-p", f"127.0.0.1::{port}", image)
    if r.returncode != 0:
        return False, f"container did not start: {r.stderr.strip()[:300]}"
    try:
        host_port = sh("docker", "port", name, str(port)).stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        user = sh("docker", "exec", name, "id", "-u").stdout.strip()
        deadline, last = time.time() + 90, ""
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{host_port}/", timeout=5) as resp:
                    body = resp.read(200000).decode("utf-8", "replace")
                    if resp.status == 200 and marker in body:
                        # The visible UI also needs its scripts and styles: every local .js/.css must load.
                        found = re.findall(r'(?:src|href)="(?!https?:|//|data:)([^"#?]+\.(?:js|mjs|css))"', body)
                        assets = sorted({"/" + a.lstrip("./").lstrip("/") for a in found})
                        bad = []
                        for a in assets:
                            try:
                                with urllib.request.urlopen(f"http://127.0.0.1:{host_port}{a}", timeout=5) as ar:
                                    if ar.status != 200 or not ar.read(1):
                                        bad.append(a)
                            except Exception:  # noqa: BLE001
                                bad.append(a)
                        if bad:
                            return False, f"page served but assets failed: {bad[:3]}"
                        return True, (f"build {built:.0f}s, HTTP 200 with marker, {len(assets)} js/css assets OK, "
                                      f"runs as uid {user or '?'}, port {port}")
                    last = f"HTTP {resp.status}, marker {'present' if marker in body else 'missing'}"
            except Exception as exc:  # noqa: BLE001 - still starting
                last = exc.__class__.__name__
            time.sleep(2)
        logs = sh("docker", "logs", "--tail", "15", name)
        return False, f"not serving the app after 90 s ({last}); logs: {(logs.stdout + logs.stderr)[-500:]}"
    finally:
        sh("docker", "rm", "-f", name)


def main(selected):
    work = Path(tempfile.mkdtemp(prefix="cs-fwtest-"))
    results = {}
    try:
        for fw in selected:
            ok, detail = run_one(fw, work)
            results[fw] = ok
            print(f"{'PASS' if ok else 'FAIL'}  node {NODE_VERSION:5} {fw:8} {FRAMEWORKS[fw]['name']:20} {detail}", flush=True)
    finally:
        for fw in selected:
            sh("docker", "rmi", "-f", f"cs-fwtest-{fw}:latest")
        shutil.rmtree(work, ignore_errors=True)
    passed = sum(results.values())
    print(f"RESULT: {passed}/{len(results)} frameworks built and served their UI")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or list(FRAMEWORKS)))
