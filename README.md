# SA Ambos Clinical Revision Hub

This repository powers the public SA Ambos Clinical Revision Hub website: https://saambosclinicalrevisionhub.github.io/quiz/

Questions are generated from content on the publicly available SA Ambulance Service Clinical website: https://clinical.saambulance.sa.gov.au/

## Important disclaimer

This quiz is for educational revision only.

It is not clinical or operational advice or a substitute for the original SA Ambulance Service Clinical website or App.

Users should always refer to the original SA Ambulance Service Clinical website/App for current and authoritative clinical information.

## How the quiz works

The site uses:

- GitHub Pages to host the public quiz website.
- GitHub Actions to run an automated update workflow.
- A Python script to crawl relevant SA Ambulance Service Clinical website content and check for any updates at 0330 daily. 
- Gemini API to generate purely source-based quiz questions, with a link back to the source document.
- JSON files in the `data` folder to store generated questions and metadata.

## Clinical levels

Questions are be generated separately for the different SAAS clinical levels.

## Included source areas

The crawler is intended to use content from:

- Clinical Practice Guidelines
- Medicines
- Calculators

## Excluded source areas

The crawler is intended to exclude:

- Tools
- Checklists
- CPPROs

Because they are not clinical level specific.
