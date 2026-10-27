# How the map works

A network of a few hundred people drawn as a free-floating web is unreadable. Lab Network draws it as a fixed
diagram with the lab at the centre, so each person's position tells you something and the picture looks the same
every time you open it.

## The lab map

![The lab map](img/lab-map.png)

- **Rings are degrees.** The lab members sit on a small circle at the centre. The first ring holds people with a
  recorded relationship to a lab member; the second ring holds people one step further out.
- **Sectors are lab members.** Each lab member owns a slice of the circle, sized by how many people are reached
  through them. Everyone in that slice is connected to the lab through that member, so the map answers "who in the lab
  could introduce me?" at a glance. The name and count of each sector are on the outer rim.
- **Each person sits beside the person who connects them.** A second-degree person is placed next to the first-degree
  person they are connected through, so clusters (a collaborator's trainees, a mentor's former lab) stay together.
  When someone has several equally short routes, the map uses the one with the most records; the person's page lists
  all of them.
- **Only the connecting lines are drawn at first.** Each person has one line toward the lab. Point at someone to see
  all their ties, or tick "Draw every tie" in the map key.
- **Colour is the kind of relationship:** orange for supervision (mentor, postdoc, committee), blue for research
  (coauthor, shared grant, collaborator), green for association (training program, lab or center colleague, NIH IRTA
  colleague, met, same event). When two people have records of several kinds, the line takes the first kind in that
  order, so a former mentor who is now a coauthor is drawn as supervision. Every colour is also written out in the key
  and on each person's page. Training-program records count as associations because most of them record co-listing on
  a program roster and say that individual supervision is not established.
- **Line style is the evidence:** solid for a public record, dashed for a reported relationship, dotted for a
  firsthand record (someone in the lab saw or took part in it).
- **Names appear as you zoom.** At the overview zoom, first-degree names are shown when there is room. Second-degree
  names appear when you zoom in, and any name appears when you point at its person.
- **Hollow markers** are people hidden by a filter who connect someone shown to the lab. The map keeps them so no route
  breaks.

## Selecting a person

Click a person, or open them from search or the People page. Their page opens on the right, and the map highlights:

- their shortest routes to the lab, drawn as thick lines;
- their direct ties;
- everyone else, faded.

![A selected person and their route to the lab](img/person.png)

Their page lists every relationship once per person, under the first kind that applies, in sections by role:
mentors, postdoc supervisors and committee members first, then mentees, postdocs and the students whose committees
they served on, then coauthors, shared grants and collaborators, then associations. Supervision and association lists
run newest first, with undated relationships last; research lists start with the people who share the most records.
The page also says whether the person's connections were collected (see "How complete the rings are").

## A person's own map

"Centre map on them" redraws the map around that person. Their direct ties form a ring, grouped into supervision
(top), research and association. On supervision lines, arrows point from the supervisor to the person supervised.
People outside the current filters have a dashed outline. Their route back to the lab continues past the ring.
Use "Lab map" in the breadcrumb or the "Lab" button to return.

![A person's own map](img/person-map.png)

## Search on the map

Typing in the search box and pressing Enter keeps you on the map: everyone linked to a match is outlined, the people
between them and the lab stay visible, and everyone else fades. A panel says how many people matched and in which
kinds of record. "Show detailed results" opens the full list: one entry per person, with their position, degree and
the people who connect them to the lab, followed by their matching research profile (the matching passage, with the
term highlighted), grants, papers and notes. People closest to the lab come first; "Most matches first" reorders them.
"By kind of record" groups the same matches into research profiles, grants, papers, notes, and names and
affiliations instead. A search that matches exactly one person's name opens that person.

![A search outlined on the map, with the panel that opens the detailed results](img/search.png)

Each paper in the results is labelled with how it links to each person: it is on their profile, it is in their
OpenAlex record (the recent papers in the research library), or it is the source of a coauthor record between them and
someone else. Grants link the same way through profiles and shared-grant records, and through the NIH and NSF awards
in the library, labelled with the person's role.

## How complete the rings are

A ring is only useful if every person in the ring before it had their connections collected to the same standard.
Two things keep that visible:

- **A degree limit.** A workspace can show only the degrees whose connections were collected
  (`labnetwork.py config WORKSPACE --degree-limit 2`). People further out are left out of the map, the People list and
  topic searches, and the counts line says how many. They can still be found by name.
- **Collection status.** `labnetwork.py expand` records, for each first-degree person, whether their coauthors were
  collected, and on what rule. The counts line above the map says how many first-degree people that covers; each
  person's page says what happened for them (collected, not found in OpenAlex, several possible matches, not looked up
  because no doctorate or research relationship is recorded, not reached because the run stopped at its spending
  limit, or undone).

## Affiliations

The Affiliations page groups the people within two degrees by institution, then department, then lab. The
institution is the one on the person's record; the department is read from their position title ("Professor of
Psychology"); the lab is the lab named on their record or a current lab roster that lists them. A lab stays whole, in
the department its faculty's titles name. Each grouping says where it came from, so check it before relying on it.

For each institution the page lists the lab members its people are reached through, the relationship records among
its people, and the other institutions linked to it by relationship records. "Show them on the map" outlines the
institution's people on the lab map. Sharing an institution is never counted as a relationship.

## Moving around

| Action | Mouse or touch | Keyboard (with the map focused) |
|---|---|---|
| Zoom | Scroll, pinch, double-click, or the + and − buttons | `+` and `-` |
| Pan | Drag | Arrow keys |
| Fit the map to the screen | ⤢ button | `0` |
| Back to the lab map | "Lab" button | `H` |
| Close a person's page | "Close", or click empty space | `Esc` |
| Search | | `/` |

Back and forward in the browser step through the people you opened, because each view has its own address.

## Filters

The bar above the map has two kinds of filter:

- **People filters** (degree range, doctorate confirmed, institution and position known) decide who is drawn and
  listed. They never change anyone's degree. Lab members are always shown. The bar says how many people in range the
  filters hide; "Show them" removes the filters.
- **Relationships** decides which records count as connections: relationship types, kinds of evidence, and whether
  proposed records count. Changing it recalculates every degree and path, so the map, search results, people list and
  paths always agree.

## The map is not the only view

The People page lists everyone with the same filters, sortable and searchable, for keyboard and screen-reader use.
Search, Paths and each person's page give the same information as text with sources.
