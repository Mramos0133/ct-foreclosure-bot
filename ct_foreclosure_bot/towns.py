"""The 169 towns/cities of Connecticut, used to drive per-town searches
against the CT Judicial Branch Property Address Search.

Search on that site does substring matching on City/Town (e.g. "Hartford"
also matches "East Hartford" and "West Hartford"), so results are filtered
back down to an exact City/Town match in search.py. Iterating this full,
non-overlapping list plus exact-match filtering plus docket-number
de-duplication keeps per-town result sets precise without missing towns
whose names are substrings of others (e.g. "Windsor" / "Windsor Locks").
"""

CT_TOWNS = [
    "Andover", "Ansonia", "Ashford", "Avon", "Barkhamsted", "Beacon Falls",
    "Berlin", "Bethany", "Bethel", "Bethlehem", "Bloomfield", "Bolton",
    "Bozrah", "Branford", "Bridgeport", "Bridgewater", "Bristol",
    "Brookfield", "Brooklyn", "Burlington", "Canaan", "Canterbury",
    "Canton", "Chaplin", "Cheshire", "Chester", "Clinton", "Colchester",
    "Colebrook", "Columbia", "Cornwall", "Coventry", "Cromwell", "Danbury",
    "Darien", "Deep River", "Derby", "Durham", "Eastford", "East Granby",
    "East Haddam", "East Hampton", "East Hartford", "East Haven",
    "East Lyme", "Easton", "East Windsor", "Ellington", "Enfield", "Essex",
    "Fairfield", "Farmington", "Franklin", "Glastonbury", "Goshen",
    "Granby", "Greenwich", "Griswold", "Groton", "Guilford", "Haddam",
    "Hamden", "Hampton", "Hartford", "Hartland", "Harwinton", "Hebron",
    "Kent", "Killingly", "Killingworth", "Lebanon", "Ledyard", "Lisbon",
    "Litchfield", "Lyme", "Madison", "Manchester", "Mansfield",
    "Marlborough", "Meriden", "Middlebury", "Middlefield", "Middletown",
    "Milford", "Monroe", "Montville", "Morris", "Naugatuck", "New Britain",
    "New Canaan", "New Fairfield", "New Hartford", "New Haven", "Newington",
    "New London", "New Milford", "Newtown", "Norfolk", "North Branford",
    "North Canaan", "North Haven", "North Stonington", "Norwalk", "Norwich",
    "Old Lyme", "Old Saybrook", "Orange", "Oxford", "Plainfield",
    "Plainville", "Plymouth", "Pomfret", "Portland", "Preston", "Prospect",
    "Putnam", "Redding", "Ridgefield", "Rocky Hill", "Roxbury", "Salem",
    "Salisbury", "Scotland", "Seymour", "Sharon", "Shelton", "Sherman",
    "Simsbury", "Somers", "Southbury", "Southington", "South Windsor",
    "Sprague", "Stafford", "Stamford", "Sterling", "Stonington", "Stratford",
    "Suffield", "Thomaston", "Thompson", "Tolland", "Torrington",
    "Trumbull", "Union", "Vernon", "Voluntown", "Wallingford", "Warren",
    "Washington", "Waterbury", "Waterford", "Watertown", "Westbrook",
    "West Hartford", "West Haven", "Weston", "Westport", "Wethersfield",
    "Willington", "Wilton", "Winchester", "Windham", "Windsor",
    "Windsor Locks", "Wolcott", "Woodbridge", "Woodbury", "Woodstock",
]

assert len(CT_TOWNS) == 169, f"expected 169 CT towns, got {len(CT_TOWNS)}"
