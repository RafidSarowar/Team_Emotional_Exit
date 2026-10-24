# Voice packs

Drop voice pack `.zip` files (or plain folders) here. Each pack has one folder per scenario `audio` key from `config.yaml`:

```
approaching/        *.wav | *.mp3 | *.ogg | *.flac
lingering/
unattended_motion/
```

The newest pack wins per scenario, and new packs are picked up without a restart. Write zips atomically (`name.zip.tmp` → rename to `name.zip`). See the main README for the full contract.
