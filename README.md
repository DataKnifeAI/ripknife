# Ripknife

![Ripknife](docs/ripknife-hero.png)

AI-automated, config-driven disc rip.

Plug in the drive and put in the disc. Ripknife does the rest.

AI and scripts detect the disc, map it, write the config, rip it, and copy it to the library.

A disc of episodes becomes one file per episode. A movie that spans discs is joined into one file, and only that joined file is copied. Editions are named so the library can show them.

## Setup

Point Ripknife at your library folders, and put an OpenRouter key in `.env`. That is the one-time setup.

How the pipeline works, and how to change this repo, is in [docs/](docs/agent.md).
