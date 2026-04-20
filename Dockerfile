# Use Node.js
FROM node:18

# Create app directory
WORKDIR /usr/src/app

# Copy everything from your repo into the container
COPY . .

# Install dependencies (This goes into your bot folder to find the package.json)
RUN cd bot && npm install

# Start the bot
CMD ["node", "bot/index.js"]
