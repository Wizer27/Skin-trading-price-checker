"""dipscan — find coins and stocks trading unusually far below their own recent range."""

from .analyze import Dip, Filters, measure, rank
from .models import Asset

__version__ = "0.1.0"
__all__ = ["Asset", "Dip", "Filters", "measure", "rank"]
