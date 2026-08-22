# Completed
- [x] Capital letters on all UI controls
- [x] NZBGet disabled by default
- [x] Collapsible sections on the Apps page
- [x] Section checkboxes enable that section's default apps
- [x] Optional NFS exports for `/media/TV`, `/media/Movies`, and `/media/downloads`
- [x] Media paths available to and pre-configured in enabled apps

# Requires Live Validation
- [ ] Finish and verify every upstream app integration on a scratch host.
  Sonarr/Radarr, Transmission/NZBGet, Prowlarr, Unpackerr, and Emby
  libraries are pre-wired. Bazarr, Recyclarr, and the
  Dispatcharr/Emby/ECM/Teamarr links still depend on upstream contracts
  that have not been exercised against running containers.
