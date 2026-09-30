# Read a Loom recording: narration and timestamped visuals

Use the shared recording URL (`https://www.loom.com/share/<video-id>`) in the
signed-in browser. Open a new tab and retain its target ID. Explicitly select
that target before each batch: another browser session can change the harness's
active tab between calls. Respect the recording's access controls.

## Transcript

Use the visible **Transcript** panel and read its rendered text. Preserve the
cue timestamps. A text-only fetch of the share page can miss this client-rendered
content. Do not interpret the narration alone as proof of a visual UI defect.

## Seek-preview sprite and timestamp map

The player exposes its seek-preview image as `img[alt="thumb"]`. Inspect its
`currentSrc`, `naturalWidth`, and `naturalHeight`. The surrounding element uses
`data-name="SeekPreviewThumbnail"` and CSS variables `--seekPreviewW` and
`--seekPreviewH` for the tile dimensions. The image is a sprite, not one frame.

The timestamp map is WebVTT. From the recording's own tab, a read-only GraphQL
request to `/graphql` can retrieve its signed URL:

```js
const videoId = location.pathname.split('/').filter(Boolean).at(-1);
const response = await fetch('/graphql', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'apollographql-client-name': 'web',
  },
  body: JSON.stringify({
    operationName: 'GetVideoSeekPreview',
    variables: { videoId, trimId: null, password: null },
    query: `query GetVideoSeekPreview($videoId: ID!, $trimId: String, $password: String) {
      getVideo(id: $videoId, password: $password) {
        ... on RegularUserVideo { seekPreviewCdnUrl(trimId: $trimId) }
      }
    }`,
  }),
});
const result = await response.json();
const vttUrl = result.data?.getVideo?.seekPreviewCdnUrl;
```

If the service rejects the request headers, inspect the page's own GraphQL
request and match its client-version header; do not depend on a hardcoded version.
Keep signed CDN URLs in memory, not in logs, screenshots, documentation, or PRs.
Fetch the VTT in the same browser context. Its cues contain the interval and
`<image>.jpg#xywh=x,y,width,height`. Match the requested time to a cue, then crop
that rectangle from the already-loaded sprite. **Do not divide duration by the
number of tiles:** cue durations can vary and repeated frames can span longer
intervals. In one verified recording the sprite was 8 columns by 25 rows of
290×140 tiles, but inspect each recording instead of assuming those dimensions.

## Full-resolution frames when thumbnails cannot show the detail

Loom can have multiple `<video>` elements, including a short preview. Inspect
`duration`, `videoWidth`, and `videoHeight` to select the actual recording. Pause
it, attach a `seeked` listener, set `currentTime`, and wait before extracting:

```js
const video = /* the actual recording element found above */;
video.pause();
await new Promise(resolve => {
  video.addEventListener('seeked', resolve, { once: true });
  video.currentTime = targetSeconds;
});
const canvas = document.createElement('canvas');
canvas.width = video.videoWidth;
canvas.height = video.videoHeight;
canvas.getContext('2d').drawImage(video, 0, 0);
const frame = canvas.toDataURL('image/png');
```

Check whether the player is already at the target time before awaiting a new
seek event. Inspect the extracted frame before drawing conclusions. A screenshot
of the share page can still show the poster while a hidden video has successfully
seeked; inspect the selected video rather than treating the poster as that frame.
If canvas access is blocked by cross-origin policy, use the visible player and
normal screenshots instead. Do not work around an access restriction.

Loom's separate **video prompts** recording mode can include keyframes, narration,
clicks, and visited URLs in an agent handoff. It is not necessarily available on
an ordinary existing recording. A **Generate** panel for summaries or bug reports
is not proof that an agent handoff has already been generated.
