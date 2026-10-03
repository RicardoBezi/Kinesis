# ADR 0007: Previews as JPEG frame sequences

**Status:** Accepted. Spike S3 passed on 2026-10-03: 0.057 s/frame on the workstation GPU and 0.417 s/frame on GPU-less GitHub CI (llvmpipe)

## Context
The reviewer must see Original, A and B in sync, frame by frame.
- Compose Desktop has no built-in video player that is accurate to the frame.
- Embedding VLC or JavaFX would add heavy dependencies.
- The multimodal evaluator also works on individual frames.

## Decision
The worker renders two camera views with the Workbench engine (solid shading, checker floor), chosen for determinism and speed:
- **Crop view:** a 512×512 JPEG for every frame of the context window. The camera is aimed at the foot's world bounding box over the window, with a 0.35 m margin.
- **Context view:** a wide shot, rendered every 4th frame.

The client fetches the frames as artifacts. A single shared frame index drives all three panes.

The evaluator receives a sample of the frames. The maximum count N is set by spike S4.

## Consequences
- Synchronization is exact and simple.
- Bandwidth and storage are higher than video. This is acceptable for a 512² crop over about 50 frames.
- An MP4 export for sharing can be added later.
