# Mission 1 methodology pitch

Offline, supervisor-facing research-notebook presentation for Mission 1. It explains the real-life trust problem, the controlled experiment assumptions, inputs, outputs, success measures, limits, and decisions requested. It intentionally avoids an architecture map or dashboard controls.

## Launch

On the remote server:

```sh
PORT=8788 ./start.sh
```

From your local machine, forward the same port:

```sh
ssh -L 8788:127.0.0.1:8788 <user>@<server>
```

Then open:

```text
http://127.0.0.1:8788/mission1_methodology_pitch.html
```

The server binds to `127.0.0.1` by default. Override it with `HOST`, and choose another port with `PORT`.
