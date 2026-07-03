# Video Audio Tool Caption Proxy

This is an optional fallback service for public YouTube captions only. It does not download video or audio and does not bypass DRM. It returns SRT text to the main Railway backend when Railway cannot access YouTube caption endpoints.

## Endpoints

- `GET /`
- `POST /extract`
- `POST /caption`
- `POST /debug`

Request:

```json
{
  "url": "https://youtu.be/VIDEO_ID",
  "language": "auto"
}
```

Success response:

```json
{
  "success": true,
  "srt_text": "1\n00:00:00,000 --> ...",
  "subtitle_source": "caption_proxy",
  "no_media_download": true
}
```

## Main backend setting

After deploying this proxy, set this variable in the Railway backend:

```text
CAPTION_PROXY_URL=https://YOUR-PROXY-DOMAIN/extract
```
