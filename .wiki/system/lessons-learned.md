# Lessons Learned / System Log

- **2026-04-20**: Initial project onboarding via AI agent. Found unstitched multi-tile microscope arrays from 2026-04-17 (over 4500 `.tif` image files). Ensured Git LFS tracking is used since TIFs are huge.
- **2026-05-25**: Implemented a standalone Qt GUI interface with a decoupled background subprocess pipeline worker. Discovered that processing 16-bit uncompressed `.tif` files directly in GUI QPixmap leads to Out-Of-Memory (OOM) crashes. Solved by rendering lightweight 8-bit `.png` preview proxies.

