# GenBox Push Sender (Development Image)

This branch adds local sender capabilities for the GenBox Push v1 contract.
An administrator can configure one destination, push an already stored image,
start a selected batch, or save a scheduled incremental Push. Live VPS and
cross-project acceptance remain separate evidence gates.

## What It Does

- Stores one GenBox destination, stable source ID, and Push key in a dedicated
  runtime file outside the ordinary settings response.
- Uses `GET /api/sync/push/status` before an image upload.
- Refuses HTTP redirects so the Push key and image cannot be forwarded to a
  different origin.
- Sends the image bytes with the canonical source SHA-256 digest.
- Accepts a result only when the receiver returns Push contract `v1`, the same
  source ID, the matching SHA-256 digest, and an accepted import status.
- Records a minimal local delivery result without persisting the Push key.

## What It Does Not Do Yet

- It does not delete or alter the source image, even when the receiver says it
  is eligible for deletion.
- It does not use a configured Push destination until the administrator
  explicitly saves the settings and starts the relevant Push action.
- It does not connect to a remote GenBox during image build or test.
- It does not expose a general remote command path.

## Runtime API

All routes require the local chatgpt2api administrator credential.

- `GET /api/genbox-push/settings`: returns safe configuration fields and only
  `has_push_key`, never the Push key itself.
- `POST /api/genbox-push/settings`: saves the destination configuration.
- `POST /api/genbox-push/probe`: checks receiver compatibility without sending
  an image.
- `POST /api/genbox-push/images`: sends one stored image selected by its
  relative gallery path.

The destination and Push key are supplied only when the owner configures the
isolated runtime. They must not be baked into an image, committed to Git, put in
a URL, or copied to ordinary logs.

The destination URL is an administrator-controlled trust boundary. Configure
only a GenBox endpoint that you own or have explicitly approved; the sender
accepts HTTP(S) syntax but never follows redirects to another origin.

## Configuration Input

The settings page accepts either a GenBox service root such as
`https://genbox.example` or the final Push endpoint copied by GenBox, such as
`https://genbox.example/api/sync/push`. The sender normalizes the latter before
probing or uploading, so it requests exactly one `/api/sync/push/status` or
`/api/sync/push` path. A URL that embeds that endpoint and adds another suffix
is rejected.

For the guided GenBox flow, paste the three exact lines copied from GenBox:

```text
GenBox Push URL: https://genbox.example/api/sync/push
Source ID: gbxps-example
Push Key: gpk-example
```

Selecting "fill configuration" parses those three fields into the form and
immediately clears the pasted text. It does not save, enable, probe, or send a
Push request. The Push key remains masked in the form until the owner explicitly
saves the configuration, and it is never placed in a URL or ordinary log.

## Immutable Image Delivery

The repository's Docker publish workflow is manual. It publishes only after a
maintainer types `publish` into its confirmation field. The workflow summary
prints an immutable `ghcr.io/...@sha256:...` reference. Use that exact reference
for a clean GenBox isolated deployment. Local Docker tags, a mutable tag such
as `latest`, and an unverified registry address are not deployment inputs.

Publishing an image is a separate owner action. Building or testing an image
locally does not create a registry artifact or authorize deployment.

## Isolated Cleanup Attestation Launch

Source deletion remains disabled unless an isolated Docker host explicitly
starts a new runtime using `scripts/start_isolated_cleanup_runtime.py`. Run the
launcher from the host, never from the application image or container. It
creates the Docker container first, reads Docker's actual container ID, calls
the sibling host-only issuer, then starts the container with the generated
capability, deployment nonce, signed attestation, and public key mounted at
`/run/genbox-cleanup` as read-only files.

The host supplies a new identity JSON and private Ed25519 key through files it
controls. The launcher checks the Docker image ID and marker hash before it
creates artifacts. The private key is passed only to the host Python issuer;
it is never part of a Docker argument, bind mount, image layer, application
environment, repository, or normal output. Use a new storage parent and a new
artifact directory for each isolated runtime. A reused marker or artifact
directory is rejected. The image argument must be a `repository@sha256:...`
reference; mutable tags are rejected. Keep the private key outside both the
artifact and storage directories because both directories are mounted into the
application container.

```text
python scripts/start_isolated_cleanup_runtime.py \
  --image registry.example/chatgpt2api@sha256:<immutable-image-id> \
  --identity-file /secure/identity.json \
  --private-key-file /secure/issuer-private.pem \
  --artifact-dir /secure/runtime-artifacts \
  --storage-parent-host /srv/genbox-isolated/data \
  --trusted-destination-kind https \
  --trusted-destination-url https://genbox.example \
  --trusted-private-host genbox.example
```

This command is a host-side provisioning step, not a browser/API action. It
does not contact GenBox, execute cleanup, or verify a VPS. On Windows run it
with the host Python and Docker Desktop; on macOS and Linux run it with the
host Python and Docker Engine/Desktop. The same identity and private-key files
must be protected by the host operator and must never be committed.
