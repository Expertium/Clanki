- The background FSRS-7 passes (predictions for the Stats graphs, automatic
  optimization) no longer write into a different profile when you switch
  profiles while they run. Before, this could happen when the two profiles
  held copies of the same collection.
