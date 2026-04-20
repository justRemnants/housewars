FROM node:20

WORKDIR /app

# Copy the entire repo
COPY . .

# Move into the bot folder and install dependencies
# CHANGE 'bot' if your folder has a different name!
RUN cd bot && npm install

# Start the bot
# CHANGE 'bot/index.js' if your path is different!
CMD ["node", "bot/index.js"]
