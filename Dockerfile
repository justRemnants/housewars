FROM node:20

WORKDIR /app

# Copy the entire repo so it sees the bot folder
COPY . .

# Go into the bot folder and install dependencies
# CHANGE 'bot' to the actual name of your folder!
RUN cd bot && npm install

# Tell it exactly how to start
# CHANGE 'bot/index.js' to your actual file path!
CMD ["node", "bot/index.js"]
