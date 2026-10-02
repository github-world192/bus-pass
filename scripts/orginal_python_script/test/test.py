# Verification Snippet (Internal Logic Check)

from optimizeVersion import CONFIG, TimeParser

# Case 1: 進站 (Should be -1, highest priority)
text_1 = "進站中"
result_1 = TimeParser.parse_text_to_seconds(text_1)

# Case 2: 將到 (Should be 0, second highest priority)
text_2 = "將到站"
result_2 = TimeParser.parse_text_to_seconds(text_2)

# Case 3: 實際秒數 (e.g., 5分, should be 300)
text_3 = "5分"
result_3 = TimeParser.parse_text_to_seconds(text_3)

# Assertions:
assert result_1 == -1, f"Expected -1 for '進站中', got {result_1}"
assert result_2 == 0, f"Expected 0 for '將到站', got {result_2}"
assert result_3 > 0, f"Expected >0 for '5分', got {result_3}"

# Sorting order check:
print(f"'進站中' ({result_1}) < '將到站' ({result_2}) < '5分' ({result_3})")
# The final result in the `plan_route` will correctly sort "進站中" first.