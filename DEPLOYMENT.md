# Deployment Guide

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) installed
- [Docker Compose](https://docs.docker.com/compose/install/) (included with Docker Desktop)

## Quick Start

```bash
docker compose up --build
```

Then open **http://localhost:8501** in your browser.

## Other Commands

**Build the image only:**
```bash
docker build -t gcode-app .
```

**Run without Docker Compose:**
```bash
docker run -p 8501:8501 gcode-app
```

**Stop the container:**
```bash
docker compose down
```

**Rebuild after code changes:**
```bash
docker compose up --build
```

## Notes

- The app is stateless — uploaded files are processed in-memory and not persisted.
- To update dependencies, edit `requirements.txt` and rebuild the image.
- The app runs on Python 3.11.
