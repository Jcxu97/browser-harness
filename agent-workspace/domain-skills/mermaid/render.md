# mermaid.live — render & validate arbitrary Mermaid

Use this when you need to **render a Mermaid diagram and confirm it actually parses**
(e.g., after editing a `.md`/`.html` that embeds Mermaid) without a local mermaid CLI.
mermaid.live renders client-side, so it doubles as a free validator.

## URL format (the non-obvious part)

The editor state lives entirely in the URL hash, **not** a query param:

```
https://mermaid.live/edit#pako:<data>
```

`<data>` is the editor state JSON, **zlib-compressed then URL-safe base64**. It is
NOT plain base64 of the code — skipping the deflate step gives a blank editor. Build it
in Python (the harness already runs Python):

```python
import json, zlib, base64

def mermaid_live_url(code: str) -> str:
    state = {"code": code, "mermaid": '{\n  "theme": "default"\n}',
             "autoSync": True, "updateDiagram": True}
    raw = json.dumps(state).encode("utf-8")
    data = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")
    return "https://mermaid.live/edit#pako:" + data
```

`zlib.compress` emits the zlib-wrapped stream `pako.inflate` expects (header + adler32) —
this matches mermaid.live's `pako.deflate`. Stripping the `=` padding is fine; the
decoder is tolerant.

## Confirming the render (headless-safe, no screenshot needed)

`capture_screenshot()` saves an image for visual inspection.
Check the page structure for a graph, then inspect the image when appearance matters.

```python
new_tab(mermaid_live_url(code)); wait_for_load()
import time; time.sleep(2)   # mermaid renders client-side after load
print(js("""(() => {
  const svgs=[...document.querySelectorAll('svg[id^="graph-"]')]
    .filter(s => s.getClientRects().length)
    .map(s=>({id:s.id,nodes:s.querySelectorAll('g').length}));
  return JSON.stringify({rendered:svgs,
    syntaxErr:/syntax error|parse error/i.test(document.body.innerText||'')});
})()"""))
# rendered:[{id:"graph-247",nodes:136}] + syntaxErr:false  => parses & renders
# rendered:[] (or only tiny svgs) + syntaxErr:true         => broken Mermaid
```

Check that the editor contains the submitted code before treating a graph as the requested result.
Use the graph ID prefix to exclude toolbar icons.
Do not reject small diagrams based on group count.

## Traps

- **`#pako:` is a hash, not a query.** `wait_for_load()` won't catch the client-side
  render — add a short `time.sleep`.
- **Do not trust error text alone.** Require a visible graph SVG that matches the submitted code.
  A blank editor (bad encoding) shows neither an error nor a graph.
- Theme/look-and-feel never affects parse success; keep the `mermaid` state minimal.
