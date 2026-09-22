{
  "patcher": {
    "fileversion": 1,
    "appversion": {
      "major": 8,
      "minor": 6,
      "revision": 5,
      "architecture": "x64",
      "modernui": 1
    },
    "classnamespace": "box",
    "rect": [
      0,
      0,
      700,
      420
    ],
    "openrect": [
      0,
      0,
      300,
      130
    ],
    "openinpresentation": 1,
    "devicewidth": 300,
    "boxes": [
      {
        "box": {
          "id": "in",
          "maxclass": "newobj",
          "text": "plugin~",
          "patching_rect": [
            20,
            20,
            320,
            22
          ],
          "numinlets": 2,
          "numoutlets": 2,
          "outlettype": [
            "signal",
            "signal"
          ]
        }
      },
      {
        "box": {
          "id": "out",
          "maxclass": "newobj",
          "text": "plugout~",
          "patching_rect": [
            20,
            65,
            320,
            22
          ],
          "numinlets": 2,
          "numoutlets": 0
        }
      },
      {
        "box": {
          "id": "loaded",
          "maxclass": "newobj",
          "text": "live.thisdevice",
          "patching_rect": [
            20,
            120,
            320,
            22
          ],
          "numinlets": 1,
          "numoutlets": 3
        }
      },
      {
        "box": {
          "id": "deferload",
          "maxclass": "newobj",
          "text": "deferlow",
          "patching_rect": [
            20,
            160,
            320,
            22
          ],
          "numinlets": 1,
          "numoutlets": 1
        }
      },
      {
        "box": {
          "id": "reader",
          "maxclass": "newobj",
          "text": "js device.js",
          "patching_rect": [
            20,
            210,
            320,
            22
          ],
          "numinlets": 1,
          "numoutlets": 1
        }
      },
      {
        "box": {
          "id": "node",
          "maxclass": "newobj",
          "text": "node.script bridge.js @autostart 1",
          "patching_rect": [
            20,
            320,
            320,
            22
          ],
          "numinlets": 1,
          "numoutlets": 2
        }
      },
      {
        "box": {
          "id": "deferrequest",
          "maxclass": "newobj",
          "text": "deferlow",
          "patching_rect": [
            380,
            260,
            320,
            22
          ],
          "numinlets": 1,
          "numoutlets": 1
        }
      },
      {
        "box": {
          "id": "title",
          "maxclass": "comment",
          "text": "POCKET / NATIVE MIDI",
          "patching_rect": [
            400,
            20,
            220,
            25
          ],
          "presentation": 1,
          "presentation_rect": [
            12,
            10,
            220,
            25
          ],
          "fontsize": 16
        }
      },
      {
        "box": {
          "id": "info",
          "maxclass": "comment",
          "text": "Explicit native MIDI observation.\nAudio passes through unchanged.\nNative writes unavailable in this build.",
          "patching_rect": [
            400,
            60,
            250,
            65
          ],
          "presentation": 1,
          "presentation_rect": [
            12,
            42,
            266,
            72
          ]
        }
      }
    ],
    "lines": [
      {
        "patchline": {
          "source": [
            "in",
            0
          ],
          "destination": [
            "out",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "in",
            1
          ],
          "destination": [
            "out",
            1
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "loaded",
            0
          ],
          "destination": [
            "deferload",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "deferload",
            0
          ],
          "destination": [
            "reader",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "reader",
            0
          ],
          "destination": [
            "node",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "node",
            0
          ],
          "destination": [
            "deferrequest",
            0
          ]
        }
      },
      {
        "patchline": {
          "source": [
            "deferrequest",
            0
          ],
          "destination": [
            "reader",
            0
          ]
        }
      }
    ],
    "dependency_cache": [
      {
        "name": "core.js",
        "type": "TEXT",
        "implicit": 1
      },
      {
        "name": "device.js",
        "type": "TEXT",
        "implicit": 1
      },
      {
        "name": "bridge.js",
        "type": "TEXT",
        "implicit": 1
      }
    ]
  }
}
