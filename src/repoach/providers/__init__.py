"""Model providers behind one protocol (SP-UNIVERSAL-PROVIDERS).

``protocol`` holds the seam, ``errors`` the error types, ``direct`` the
in-process providers and ``select`` the route resolution.  Nothing is
imported here: the agent loop's error types load without the transports.
"""
