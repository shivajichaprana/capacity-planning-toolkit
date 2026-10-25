"""Make the packages importable from a plain checkout.

pytest imports this file as a top-level module, which puts the directory it
sits in at the front of ``sys.path``; that is what lets ``import models`` work
without installing the project first. It is deliberately empty of fixtures. A
fixture defined here would be available to every test without being imported,
and a test that needs a series should name the builder it used -- the suite's
whole argument is that a result is only as good as the window it was derived
from, so the window has to be visible in the test that reads it.
"""
