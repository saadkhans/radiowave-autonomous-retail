"""Replaceable sensor adapters. Foundation v0 ships mock adapters only.

Each modality defines a native sample contract in ``base.py``; real vendor
integrations (radar, RFID reader, camera) will implement the same ``*Source``
protocol and never touch the rest of the system.
"""
