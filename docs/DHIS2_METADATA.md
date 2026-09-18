# Finding the UIDs for your own DHIS2 instance

Every UID is specific to one database. Copying them from another notebook is the
most common cause of silent failure, because DHIS2 rejects unknown UIDs with
messages that do not name the offending field.

Use `discover_program()`, or these API calls directly. All work in a browser
while logged into DHIS2.

## Organisation unit

    /api/organisationUnits?filter=name:eq:Ngelehun CHC&fields=id,name

## Tracker programmes on the server

    /api/programs?filter=programType:eq:WITH_REGISTRATION&fields=id,name,trackedEntityType[id,name]&paging=false

## What a programme accepts

    /api/programs/{PROGRAM_UID}?fields=programTrackedEntityAttributes[mandatory,trackedEntityAttribute[id,name,valueType,generated]]

Read both flags:

- `mandatory` — you must supply it, unless
- `generated` — the server creates it and will reject a value you send

## Programme stages and data elements

    /api/programs/{PROGRAM_UID}?fields=programStages[id,name,programStageDataElements[dataElement[id,name,valueType]]]

Clinical observations belong here as data elements, not as tracked entity
attributes.

## Reading a record back

    /api/tracker/trackedEntities?orgUnit={OU}&program={PROG}&ouMode=SELECTED&filter={ATTR_UID}:eq:{VALUE}&fields=trackedEntity,createdAt,attributes[displayName,value]

2.40 returns the list under `instances`; later versions use `trackedEntities`.
Handle both.

Filter on a value you supplied. Do not sort by recency and assume the newest
record is yours — on the public demo it very often is not.
