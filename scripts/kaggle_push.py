"""Re-push the bridge into the Kaggle notebook (restarts the backend). Used by .github/workflows/kaggle-push.yml.
Env: KAGGLE_API_TOKEN (Kaggle CLI token), BRIDGE_KEY. Optional: KAGGLE_KERNEL (default adarshstar/new-benchmark-task-68ca6).
Pulls the existing notebook (keeps its settings/image/internet flag), replaces its single code cell with kaggle/bridge.py
(+ BRIDGE_KEY default + keep-alive loop) and pushes. The key is only written into the private notebook, never printed."""
import json, os, pathlib, subprocess, sys, tempfile

kid = os.environ.get("KAGGLE_KERNEL", "adarshstar/new-benchmark-task-68ca6")
key = os.environ["BRIDGE_KEY"]
d = pathlib.Path(tempfile.mkdtemp())


def run(*a):
    print("$", " ".join(a), flush=True)
    subprocess.run(a, check=True)


run("kaggle", "kernels", "pull", kid, "-p", str(d), "-m")
nb = next(d.glob("*.ipynb"))
j = json.loads(nb.read_text())
code = ("import os, time\nos.environ.setdefault(\"BRIDGE_KEY\", " + repr(key) + ")\n"
        + pathlib.Path("kaggle/bridge.py").read_text() + "\n\nwhile True:\n    time.sleep(3600)\n")
j["cells"] = [{"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
               "source": code.splitlines(True)}]
nb.write_text(json.dumps(j))
run("kaggle", "kernels", "push", "-p", str(d))
print("pushed", kid)
