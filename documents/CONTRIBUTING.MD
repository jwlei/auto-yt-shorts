# Contributing to Auto YT Shorts

Thanks for checking this out! We love contributions—whether it's fixing bugs, adding cool features (like background music or auto-captions), or improving docs.

## ⚡ Quick Start

1.  **Fork & Clone:**

    ```bash
    git clone https://github.com/jwlei/auto-yt-shorts.git
    cd auto-yt-shorts
    ```

2.  **Install Dependencies:**

    ```bash
    pip install -r requirements.txt
    ```

3.  **Setup Keys:**
    Create a `.env` file and add your keys (see `.env.example`):

    ```ini
    GEMINI_API_KEY=your_key
    VIDEO_PROVIDER=pixabay   # or: pexels
    PIXABAY_API_KEY=your_key # or PEXELS_API_KEY when VIDEO_PROVIDER=pexels
    ```

4.  **FFmpeg Requirement:**
    You must use **FFmpeg 4.3+** (2020 or newer) for transitions to work. Old versions will crash.

## 🚀 How to Submit

1.  Create a branch: `git checkout -b feature/cool-new-thing`
2.  Make your changes and test by running `python main.py`.
3.  Push to your fork and open a **Pull Request**.

**Note:** Please do not commit your `.env` file or API keys!

Happy Coding!
