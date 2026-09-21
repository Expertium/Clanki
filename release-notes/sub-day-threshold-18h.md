- An answer button now stays in the same-day queue up to 18 hours, instead of
  12. An interval between 12 and 18 hours used to round up to one day; it is
  now scheduled for later the same day, to the second. Intervals of 18 hours
  or more still get whole days. As before, a same-day interval that reaches
  past the day rollover leaves the card due at the rollover.
