"""Rendered capacity reviews.

This package turns the typed results the models and the recommendation produce
into something a person reads in a review meeting, and it does so under one
rule: **it computes nothing**. Every number in a rendered review is a field of
a result object, formatted. No averages are taken here, no thresholds applied,
no figures derived. That rule is what makes a review auditable -- any number in
it can be traced to the object that produced it and re-derived from the
observation store -- and it is the reason the renderer is a separate package
rather than a method on each result.

The second rule follows from the first: **every finding is rendered**. A review
that shows a recommendation without the objections recorded alongside it is
more confident than its evidence, and the objections are exactly what a
reviewer is there to weigh. Findings are grouped by severity and ordered so
that errors are read before the conclusion they qualify.

Generated reviews are written into this directory by default and are ignored by
version control; the modules here are the generator, not its output.
"""

__all__ = ["review"]
