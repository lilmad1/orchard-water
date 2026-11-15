"""Put a true-colour picture together from the day's pictures, square by square.

earth_search.py fetches the colour pictures; this file only arranges them. It does no
internet work and no greenness math. The colours are for looking at (a website picture),
never for NDVI.

The choice of which picture each square comes from is not made here. It was made by
earth_search.picture_for_day and recorded in Picture.source_picture; this file copies
that record, so the colour picture can never disagree with the numbers.
"""

import numpy as np

from orchardwater.ndvi import Picture


def assemble_visual(
    visuals: list[np.ndarray], picture: Picture, margin_squares: int = 0
) -> np.ndarray:
    """Build one colour picture from several, using the record of where each square came from.

    visuals: one (height + 2 x margin, width + 2 x margin, 3) uint8 array per picture of the
        day, in the SAME order as picture.picture_ids (newest first), as read by
        earth_search.read_visual with the same margin_squares.
    picture: the day's Picture from earth_search.picture_for_day.
    Returns an array of the same shape. Inside the field's box each square takes the colour
    of the picture picture.source_picture names; a square no picture covered (source -1) is
    left black on purpose. The margin around the box always takes the newest picture, because
    the record only covers the field's box. The squares are copied exactly: a colour value of 0
    is never taken to mean "no data".
    """
    if picture.source_picture is None:
        raise ValueError("This Picture does not record where each square came from.")
    if len(visuals) == 0 or len(visuals) != len(picture.picture_ids):
        raise ValueError("There must be one colour picture for each picture id, in the same order.")
    source = np.array(picture.source_picture, dtype=int)
    height, width = source.shape
    expected = (height + 2 * margin_squares, width + 2 * margin_squares, 3)
    for visual in visuals:
        if visual.shape != expected:
            raise ValueError(f"A colour picture has shape {visual.shape}; expected {expected}.")
    result = visuals[0].copy()
    # A view: writing into it changes result. It is the field's own box inside the margin.
    box = result[margin_squares : margin_squares + height, margin_squares : margin_squares + width]
    for position in range(1, len(visuals)):
        from_here = source == position
        older_box = visuals[position][
            margin_squares : margin_squares + height, margin_squares : margin_squares + width
        ]
        box[from_here] = older_box[from_here]
    box[source == -1] = 0  # no picture had data here: black, not a guess from another picture
    return result
