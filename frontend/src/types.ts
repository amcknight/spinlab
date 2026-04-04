/** Any object with segment-naming fields (used by segmentName/shortSegName). */
export interface SegmentLike {
  description?: string;
  level_number: number;
  start_type: string;
  start_ordinal: number;
  end_type: string;
  end_ordinal: number;
}
