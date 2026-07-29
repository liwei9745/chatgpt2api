# GenBox Push Sender (Development Image)

This branch adds the first local-only sender capability for the GenBox Push v1
contract. It is intentionally limited to an administrator selecting one already
stored image for delivery.

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
- It does not add automatic per-generation Push, batch Push, or scheduling.
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

## Immutable Image Delivery

The repository's Docker publish workflow is manual. It publishes only after a
maintainer types `publish` into its confirmation field. The workflow summary
prints an immutable `ghcr.io/...@sha256:...` reference. Use that exact reference
for a clean GenBox isolated deployment. Local Docker tags, a mutable tag such
as `latest`, and an unverified registry address are not deployment inputs.

Publishing an image is a separate owner action. Building or testing an image
locally does not create a registry artifact or authorize deployment.
