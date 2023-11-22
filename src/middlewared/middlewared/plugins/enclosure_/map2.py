import logging

from .constants import HEAD_UNIT_DISK_SLOT_START_NUMBER
from .enums import ControllerModels

logger = logging.getLogger(__name__)


def to_ignore(enclosure):
    if not enclosure['controller']:
        # this is a JBOD and doesn't need to
        # be "combined" into any other object
        return True
    elif enclosure['model'].startswith((
        ControllerModels.F60.value,
        ControllerModels.F100.value,
        ControllerModels.F130.value,
        ControllerModels.R30.value,
    )):
        # these are all nvme flash systems and
        # are treated as-is
        return True
    else:
        return False


def combine_enclosures(enclosures):
    """Purpose of this function is to combine certain enclosures
    Array Device Slot elements into 1. For example, the MINIs/R20s
    have their disk drives spread across multiple enclosures. We
    need to map them all into 1 unit. Another example is that we
    have platforms (M50/60, R50B) that have rear nvme drive bays.
    NVMe doesn't get exposed via a traditional SES device because,
    well, it's nvme. So we create a "fake" nvme "enclosure" that
    mimics the drive slot information that a traditional enclosure
    would do. We take these enclosure devices and simply add them
    to the head-unit enclosure object.

    NOTE: The array device slots have already been mapped to their
    human-readable slot numbers. That logic is in the `Enclosure`
    class in "enclosure_/enclosure_class.py"
    """
    head_unit_idx, to_combine, to_remove = None, dict(), list()
    for idx, enclosure in enumerate(enclosures):
        if to_ignore(enclosure):
            continue
        elif enclosure['elements']['Array Device Slot'].get(HEAD_UNIT_DISK_SLOT_START_NUMBER):
            # the enclosure object whose disk slot has number 1
            # will always be the head-unit
            head_unit_idx = idx
        else:
            to_combine.update(enclosure['elements'].pop('Array Device Slot', dict()))
            to_remove.append(idx)

    if head_unit_idx is not None:
        enclosures[head_unit_idx]['elements']['Array Device Slot'].update(to_combine)
        enclosures[head_unit_idx]['elements']['Array Device Slot'] = {
            k: v for k, v in sorted(enclosures[head_unit_idx]['elements']['Array Device Slot'].items())
        }
        for idx in reversed(to_remove):
            # we've combined the enclosures into the
            # main "head-unit" enclosure object so let's
            # remove the objects we combined from
            enclosures.pop(idx)