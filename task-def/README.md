# RPi Camera Lattice integration tasks

Protobuf definitions of the tasks the camera agent accepts, published to the
Lattice Schema Registry (LSR) with the Buf CLI.

| Message | Type URL | Meaning |
|---|---|---|
| `Start` | `type.googleapis.com/anduril.sample_app_rpi_cam.camera.v1alpha.Start` | start the video stream |
| `Stop`  | `type.googleapis.com/anduril.sample_app_rpi_cam.camera.v1alpha.Stop`  | stop the video stream |

Both messages are empty; the agent dispatches on the type URL alone.

## Publish

```bash
# 1) Create the repository once in the LSR dashboard
#    (https://schema-registry.developer.anduril.com): owner `anduril`, name `sample-app-rpi-cam`,
#    private. If you use an organization instead, see buf.yaml.
# 2) Authenticate with a token created under Settings in the dashboard
#    (keep it out of the repo and shell history).
export BUF_TOKEN=<token>@schema-registry.developer.anduril.com
buf registry whoami schema-registry.developer.anduril.com
# 3) Validate and push from this directory.
cd task-def
buf lint
buf build
buf push
```

Publishing registers the definitions for use in Sandboxes automatically.

The LSR enforces breaking-change detection. Add fields with new numbers and never
rename, renumber, retype or remove a field. For a breaking change, create a new
version directory and package (`v2alpha`) instead of editing `v1alpha`.
