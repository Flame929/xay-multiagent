import importlib.util
import shutil
import subprocess
import sys
from io import BytesIO
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("xay_watch_chars", ROOT / "scripts" / "xay_watch.py")
WATCH = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WATCH
SPEC.loader.exec_module(WATCH)


class CharRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 7878

    def get(self, path):
        handler = object.__new__(WATCH.Handler)
        handler.path = path
        handler.headers = {"Host": "localhost:7878"}
        handler.server = type("Server", (), {"server_address": ("127.0.0.1", self.port)})()
        handler.wfile = BytesIO()
        result = {"status": None, "headers": {}}
        handler.send_response = lambda status: result.update(status=status)
        handler.send_header = lambda key, value: result["headers"].update({key: value})
        handler.end_headers = lambda: None
        handler.do_GET()
        raw = handler.wfile.getvalue()
        if result["headers"].get("Content-Type", "").startswith("image/"):
            return result["status"], result["headers"].get("Content-Type"), raw
        return result["status"], result["headers"].get("Content-Type"), raw.decode("utf-8")

    def test_gallery_and_core_routes(self):
        status, content_type, body = self.get("/chars")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        for agent in ("codex", "dsh", "agy", "claude"):
            self.assertIn("'" + agent + "'", body)
        self.assertIn("fetch('/xay-chars/' + agent + '.js'", body)
        status, content_type, body = self.get("/xay-chars/core.js")
        self.assertEqual(status, 200)
        self.assertIn("application/javascript", content_type)
        self.assertIn("window.XayChars", body)

    def test_invalid_or_missing_script_is_404(self):
        for path in ("/xay-chars/../x.js", "/xay-chars/A.js", "/xay-chars/x.txt", "/xay-chars/nope.js"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_avatar_images_route(self):
        status, content_type, body = self.get("/xay-chars/img/dsh-calm.webp")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "image/webp")
        self.assertEqual(body[8:12], b"WEBP")
        for path in ("/xay-chars/img/../core.js", "/xay-chars/img/x.png", "/xay-chars/img/DSH-calm.webp", "/xay-chars/img/nope.webp"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_character_files_use_existing_avatars(self):
        img = ROOT / "scripts" / "xay-chars" / "img"
        self.assertTrue((img / "LICENSE.md").is_file())
        faces = ("calm", "smile", "serious", "surprised", "pout", "eyesclosed", "aggrieved", "shy")
        for agent in ("codex", "dsh", "agy", "claude"):
            source = (ROOT / "scripts" / "xay-chars" / (agent + ".js")).read_text(encoding="utf-8")
            self.assertIn("XayChars.register('%s'" % agent, source)
            for face in faces:
                self.assertIn("'%s'" % face, source)
                self.assertTrue((img / ("%s-%s.webp" % (agent, face))).is_file(), (agent, face))

    @unittest.skipUnless(shutil.which("node"), "node 未安装")
    def test_image_character_in_node(self):
        node = subprocess.run(["node", "-e", """
const fs = require('fs'), vm = require('vm');
const style = { textContent: '' };
global.document = { head: { appendChild() {} }, getElementById() { return null; }, createElement() { return style; }, querySelectorAll() { return []; } };
global.window = { setTimeout() {} };
vm.runInThisContext(fs.readFileSync('scripts/xay-chars/core.js', 'utf8'));
global.XayChars = window.XayChars;
vm.runInThisContext(fs.readFileSync('scripts/xay-chars/claude.js', 'utf8'));
const s = window.XayChars.render('claude', { state: 'sleeping', size: 64 });
if (!s.includes('xc-img') || !s.includes('data-state="sleeping"')) process.exit(1);
for (const e of window.XayChars.EXPRESSIONS) if (!s.includes('data-e="' + e + '"')) process.exit(2);
if (!s.includes('/xay-chars/img/claude-eyesclosed.webp') || !s.includes('/xay-chars/img/claude-serious.webp')) process.exit(3);
if (window.XayChars.info('claude').name !== 'Claude 娘') process.exit(4);
"""], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(node.returncode, 0, node.stderr)

    @unittest.skipUnless(shutil.which("node"), "node 未安装")
    def test_core_smoke_in_node(self):
        node = subprocess.run(["node", "-e", """
const fs = require('fs'), vm = require('vm');
const style = { textContent: '' };
global.document = { head: { appendChild() {} }, getElementById() { return null; }, createElement() { return style; }, querySelectorAll() { return []; } };
global.window = { setTimeout() {} };
vm.runInThisContext(fs.readFileSync('scripts/xay-chars/core.js', 'utf8'));
const s = window.XayChars.render('missing', { state: 'idle', expression: 'neutral', size: 64 });
if (!s.includes('xc') || !s.includes('viewBox="0 0 120 140"')) process.exit(1);
for (const e of window.XayChars.EXPRESSIONS) if (!s.includes('data-e="' + e + '"')) process.exit(2);
const attrs = {}, el = { setAttribute(k, v) { attrs[k] = v; }, removeAttribute(k) { delete attrs[k]; }, getAttribute() { return 'missing'; } };
window.XayChars.setState(el, 'working');
if (attrs['data-state'] !== 'working' || attrs['data-expr'] !== 'focused' || attrs['data-gesture'] !== 'typing') process.exit(3);
"""], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(node.returncode, 0, node.stderr)


if __name__ == "__main__":
    unittest.main()
