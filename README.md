# BallsDex Suggestions Pack

A community suggestion package for **BallsDex**. Suggest new countryballs, cards, and economies,
vote and comment on them, and let staff review and moderate submissions from a single menu.

## Commands

| Command | Description |
|---|---|
| `/suggestions` | Open the Community Suggestions menu; `type` picks Countryball, Card, or Economy (defaults to Countryball). Browse, upvote, comment, and submit new suggestions from here. |

## Installation

### 1 — Configure extra.toml

**If the file doesn't exist:** Create a new file `extra.toml` in your `config` folder under the
BallsDex directory.

**If you already have other packages installed:** Simply add the following configuration to your
existing `extra.toml` file. Each package is defined by a `[[ballsdex.packages]]` section, so you
can have multiple packages installed.

Add the following configuration:

```toml
[[ballsdex.packages]]
location = "git+https://github.com/hiboman/BallsDex-Suggestions-Pack.git@0.0.1"
path = "suggestions"
enabled = true
```

**Example of multiple packages:**

```toml
# First package
[[ballsdex.packages]]
location = "git+https://github.com/example/other-package.git"
path = "other"
enabled = true

# Suggestions Package
[[ballsdex.packages]]
location = "git+https://github.com/hiboman/BallsDex-Suggestions-Pack.git@0.0.1"
path = "suggestions"
enabled = true
```

### 2 — Rebuild and start the bot

`docker compose up -d --build`

This will install the package and start the bot. 