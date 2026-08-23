#!/usr/bin/env bash
# Shared NFS mount options for Kine (mount-media.sh and nfs-browse-agent).
KINE_NFS_FSTAB_OPTS="rw,nofail,_netdev,noatime,nolock,intr,tcp,actimeo=1800"
