- The collection file is read through a memory map: large reads, such as a sorted
  browser search or the deck list after other screens, are faster (a sorted
  search of a 159k-card collection: 430 to 286 ms). The pages share the
  system's file cache, so this adds no private memory.
