# Devcontainer notes

Supplementary notes that don't belong in the main `README.md`. Currently: volume-naming
considerations for the shared Claude Code config.

## Claude Code volume namespacing on shared machines

`devcontainer.json` and `kde/devcontainer.json` both mount the same fixed-name Docker volume,
`calendar-info-claude-shared`, at `/home/ubuntu/.claude` (`CLAUDE_CONFIG_DIR`). That's
deliberate: it's what lets Claude Code's login, memory and session transcripts carry over when
you switch between the default and KDE containers for this plugin. See `README.md`'s "Claude
Code" section for the day-to-day behavior and the one-time re-auth you get the first time you
switch to this scheme.

**This is safe by default** because Docker named volumes are local to whichever Docker daemon
the container runs on. Working on this repo from two different computers never shares anything
between them - each machine has its own daemon and its own `calendar-info-claude-shared` volume.

**The gap** is a fixed literal name has no notion of *which user* or *which checkout* it belongs
to. That only matters if the same Docker daemon is shared by more than one person or more than
one clone of this repo - e.g. a persistent workstation or bare-metal server that multiple
engineers SSH into and each open VS Code Remote against with their own checkout. In that setup,
every checkout's containers would resolve to the exact same volume name and land on one shared
Claude login/memory, unintentionally.

This does **not** apply to CI/CD as typically structured: runners are ephemeral (fresh
container/VM per job, torn down after), and even a persistent self-hosted runner wouldn't have
valid Claude Code credentials in the volume to begin with - signing in requires the interactive
browser OAuth flow (`claude` run by a human, pasting the code back), which no pipeline does. A CI
job hitting this mount just finds it empty; there's nothing sensitive to leak because nothing
ever populated it there.

### If you do work on a shared/multi-user Docker host

Scope the volume name per user by adding `${localEnv:USER}` to the `source=` value in **both**
`devcontainer.json` and `kde/devcontainer.json` (they must match, or sharing between the two
configs breaks):

```
source=calendar-info-claude-${localEnv:USER},target=/home/ubuntu/.claude,type=volume
```

That closes the "different people, same box" case. It does **not** close "same person, two
clones of this repo, same box" if both clones happen to sit in identically-named folders (e.g.
`~/work/net_red-tux_calendar_info` and `~/personal/net_red-tux_calendar_info`) - Docker volume
names can't embed a full path, only components like `${localWorkspaceFolderBasename}`, and two
differently-located clones can still share a basename. Closing that fully would mean hashing the
absolute workspace path in a startup script and keying a subdirectory of one shared volume off
the hash, rather than naming the volume itself - a bigger change (new script logic, a one-time
migration of the current volume's contents, symlink setup on every container start) that trades
a low-probability, self-avoidable collision (just give clones distinct folder names) for real
added complexity.

### Status

Not implemented - the default assumption (one interactive user per machine, or clones named
distinctly) holds for the common case. Revisit the `${localEnv:USER}` change if this plugin ever
gets developed from a genuinely shared interactive box.
