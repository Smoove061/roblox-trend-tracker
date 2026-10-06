# Endpoint probe — 2026-10-06T20:14:37Z

| Endpoint | Result | Shape |
|---|---|---|
| games (details) | OK (0 universe objects) | `{data:[{id, rootPlaceId, name, description, sourceName, sourceDescription, creator:{id, name, type, isRNVAccount, hasVerifiedBadge}, price, allowedGearGenres:[…], allowedGearCategories:[], isGenreEnforced, copyingAllowed, playing, visits}]}` |
| votes | OK (0 universe objects) | `{data:[{id, upVotes, downVotes}]}` |
| explore get-sorts | OK (433 universe objects) | `{header:{sorts, layoutData:{}}, sorts:[{contentType, gameSetTypeId, gameSetTargetId, primarySortId, secondarySortId, id, sortId, sortDisplayName, topicLayoutData:{}, treatmentType, filters:[…]}], nextSortsPageToken}` |
| explore get-sort-content | OK (96 universe objects) | `{contentType, gameSetTypeId, gameSetTargetId, primarySortId, secondarySortId, sortId, sortDisplayName, subtitle, topicLayoutData:{infoText, playButtonStyle}, treatmentType, games:[{universeId, rootPlaceId, name, playerCount, totalUpVotes, totalDownVotes, isSponsored, nativeAdData, minimumAge, ageRecommendationDisplayName, contentMaturity, genreL1}]}` |
| omni-search | OK (40 universe objects) | `{searchResults:[{contentGroupType, contents:[…], topicId}], nextPageToken, filteredSearchQuery, vertical, sorts:[{topicId, treatmentType, feedItemKey, topicLayoutData:{componentType, numOfRows, minNumOfRows, enableCollectionLayout, containedTile, enableCollectionGrid}}], paginationMethod, sdui}` |
| place -> universe | OK (1 universe objects) | `{universeId}` |
| game passes | OK (0 universe objects) | `{gamePasses:[{id, productId, name, isForSale, price, userBasePriceInRobux, priceDiscountDetails:[], isOwned, creator:{creatorType, creatorId, name, deprecatedId}, displayName, displayDescription, displayIconImageAssetId, created, updated}], nextPageToken}` |
| badges | OK (0 universe objects) | `{previousPageCursor, nextPageCursor, data:[{id, name, description, displayName, displayDescription, enabled, iconImageId, displayIconImageId, created, updated, statistics:{pastDayAwardedCount, awardedCount, winRatePercentage}, awardingUniverse:{id, name, rootPlaceId}}]}` |
| rolimons gamelist | OK (0 universe objects) | `{success, game_count, games:{1818:[str], 14403:[str], 25415:[str], 47324:[str], 189707:[str], 192800:[str], 214169:[str], 330361:[str], 1600503:[str], 2649920:[str], 3237168:[str], 3435799:[str], 4060866:[str], 4927387:[str]}}` |
