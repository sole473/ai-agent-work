---
name: music-playlists
description: Search the configured music library and help the user review and save an ordered playlist.
---

使用 `music_search_tracks` 按歌名、歌手或专辑搜索。使用工具实际返回的歌曲 ID，按用户要求增删或排序。遇到空结果或服务不可用时，说明结果并请用户调整搜索条件。

保存前展示歌单名称和曲目顺序。用户确认后调用 `music_create_playlist`，并返回工具报告的结果。用户询问已有歌单时调用 `music_list_playlists`。
