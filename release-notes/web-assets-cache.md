- The deck list and a deck's overview load faster: Clanki's own scripts and
  style sheets (jQuery, jQuery UI, the heatmap's calendar) are kept in the
  web view's memory cache for the session instead of being fetched and
  compiled again on every page load (about 310 to 180 ms per deck list).
